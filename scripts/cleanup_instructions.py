#!/usr/bin/env python3
"""
临时清理脚本：
1. 每个图片的 instructions.json 最多保留 2 条 remove + 2 条 replace 指令
2. 检查 triplets/{id}/edited/{edit_id}/ 目录，删除不是 6 张 png 的编辑结果
3. 清理后检查关联性：如果 edited 中的 instruction 不在 instructions.json 中，也删除该 edit

用法:
    python scripts/cleanup_instructions.py --dry-run    # 预览将要删除的内容
    python scripts/cleanup_instructions.py              # 实际执行删除
"""

import json
import shutil
import argparse
from pathlib import Path
from collections import defaultdict
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load config for pipeline_dir
import sys

sys.path.insert(0, str(PROJECT_ROOT))
from utils.config import load_config

_config = load_config()
_pipeline_dir_raw = _config.workspace.pipeline_dir
PIPELINE_DIR = (
    Path(_pipeline_dir_raw)
    if Path(_pipeline_dir_raw).is_absolute()
    else PROJECT_ROOT / _pipeline_dir_raw
)
IMAGES_DIR = PIPELINE_DIR / "images"
TRIPLETS_DIR = PIPELINE_DIR / "triplets"


def _rel_path(p: Path) -> str:
    """Return URL-friendly path for logging."""
    try:
        return str(p.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        pass
    try:
        rel = str(p.relative_to(PIPELINE_DIR)).replace("\\", "/")
        return f"pipeline/{rel}"
    except ValueError:
        pass
    return str(p).replace("\\", "/")


# 预期的 6 个视角
EXPECTED_VIEWS = ["front", "back", "right", "left", "top", "bottom"]


def load_instructions(json_path: Path) -> list:
    """加载 instructions.json"""
    if not json_path.exists():
        return []
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception as e:
        print(f"  [WARN] Failed to load {json_path}: {e}")
    return []


def save_instructions(json_path: Path, instructions: list):
    """保存 instructions.json"""
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(instructions, f, ensure_ascii=False, indent=2)


def filter_instructions(
    instructions: list, max_remove: int = 2, max_replace: int = 2
) -> tuple:
    """
    过滤指令，最多保留 max_remove 条 remove + max_replace 条 replace
    返回 (保留的指令列表, 被删除的指令列表)
    """
    remove_list = []
    replace_list = []
    other_list = []

    for instr in instructions:
        if isinstance(instr, dict):
            instr_type = instr.get("type", "").lower()
            if instr_type == "remove":
                remove_list.append(instr)
            elif instr_type == "replace":
                replace_list.append(instr)
            else:
                other_list.append(instr)
        else:
            # 兼容旧格式（纯文本）
            other_list.append(instr)

    kept = remove_list[:max_remove] + replace_list[:max_replace]
    removed = remove_list[max_remove:] + replace_list[max_replace:] + other_list

    return kept, removed


def get_instruction_text(instr) -> str:
    """从指令对象中提取文本"""
    if isinstance(instr, dict):
        return instr.get("text", "").strip()
    return str(instr).strip()


def check_edit_dir(edit_dir: Path) -> dict:
    """
    检查 edit 目录的状态
    返回 {"valid": bool, "png_count": int, "instruction": str, "meta_exists": bool}
    """
    result = {
        "valid": True,
        "png_count": 0,
        "instruction": "",
        "meta_exists": False,
        "reason": "",
    }

    # 检查 png 文件数量
    png_files = list(edit_dir.glob("*.png"))
    result["png_count"] = len(png_files)

    if len(png_files) != 6:
        result["valid"] = False
        result["reason"] = f"Expected 6 png files, found {len(png_files)}"

    # 检查 meta.json
    meta_path = edit_dir / "meta.json"
    if meta_path.exists():
        result["meta_exists"] = True
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
                result["instruction"] = meta.get("instruction", "").strip()
        except Exception as e:
            result["valid"] = False
            result["reason"] = f"Failed to read meta.json: {e}"
    else:
        # 没有 meta.json 也视为无效
        result["valid"] = False
        result["reason"] = "Missing meta.json"

    return result


def cleanup(dry_run: bool = True):
    """执行清理"""
    print("=" * 60)
    print(f"Instructions Cleanup Script")
    print(
        f"Mode: {'DRY RUN (no changes)' if dry_run else 'EXECUTE (will delete files)'}"
    )
    print(f"Time: {datetime.now().isoformat()}")
    print("=" * 60)

    stats = {
        "images_scanned": 0,
        "instructions_removed": 0,
        "edits_deleted_incomplete": 0,
        "edits_deleted_orphan": 0,
        "images_with_excess_instructions": 0,
    }

    # 收集所有要删除的内容
    to_delete_edits = []  # [(edit_dir, reason), ...]
    to_update_instructions = []  # [(json_path, kept, removed), ...]

    # ========== Phase 1: 扫描并收集需要清理的内容 ==========
    print("\n[Phase 1] Scanning images for excess instructions...")

    image_instruction_map = {}  # image_id -> [kept instruction texts]

    for image_dir in sorted(IMAGES_DIR.iterdir()):
        if not image_dir.is_dir():
            continue

        image_id = image_dir.name
        stats["images_scanned"] += 1

        json_path = image_dir / "instructions.json"
        instructions = load_instructions(json_path)

        if len(instructions) <= 4:
            # 2 remove + 2 replace = 4，没超就跳过
            kept_texts = [get_instruction_text(i) for i in instructions]
            image_instruction_map[image_id] = kept_texts
            continue

        # 需要过滤
        kept, removed = filter_instructions(instructions, max_remove=2, max_replace=2)

        if removed:
            stats["images_with_excess_instructions"] += 1
            stats["instructions_removed"] += len(removed)
            to_update_instructions.append((json_path, kept, removed))
            print(
                f"  [{image_id}] {len(instructions)} instructions -> keep {len(kept)}, remove {len(removed)}"
            )

        kept_texts = [get_instruction_text(i) for i in kept]
        image_instruction_map[image_id] = kept_texts

    # ========== Phase 2: 扫描 edited 目录 ==========
    print("\n[Phase 2] Scanning edited directories for incomplete/orphan edits...")

    for triplet_dir in sorted(TRIPLETS_DIR.iterdir()):
        if not triplet_dir.is_dir():
            continue

        model_id = triplet_dir.name
        edited_dir = triplet_dir / "edited"

        if not edited_dir.exists():
            continue

        # 获取这个 model 对应的有效指令列表
        valid_instructions = image_instruction_map.get(model_id, [])

        for edit_id_dir in sorted(edited_dir.iterdir()):
            if not edit_id_dir.is_dir():
                continue

            edit_status = check_edit_dir(edit_id_dir)

            # 检查1: png 文件数量不是 6
            if edit_status["png_count"] != 6:
                to_delete_edits.append(
                    (edit_id_dir, f"Incomplete: {edit_status['png_count']}/6 views")
                )
                stats["edits_deleted_incomplete"] += 1
                print(
                    f"  [{model_id}/{edit_id_dir.name}] DELETE: {edit_status['reason']}"
                )
                continue

            # 检查2: instruction 是否在保留列表中
            edit_instruction = edit_status["instruction"]
            if edit_instruction and valid_instructions:
                if edit_instruction not in valid_instructions:
                    to_delete_edits.append(
                        (edit_id_dir, f"Orphan: instruction not in kept list")
                    )
                    stats["edits_deleted_orphan"] += 1
                    print(
                        f"  [{model_id}/{edit_id_dir.name}] DELETE: instruction removed from instructions.json"
                    )

    # ========== Phase 3: 执行清理 ==========
    print("\n[Phase 3] Cleanup summary...")
    print(f"  Images scanned: {stats['images_scanned']}")
    print(
        f"  Images with excess instructions: {stats['images_with_excess_instructions']}"
    )
    print(f"  Instructions to remove: {stats['instructions_removed']}")
    print(f"  Edit dirs to delete (incomplete): {stats['edits_deleted_incomplete']}")
    print(f"  Edit dirs to delete (orphan): {stats['edits_deleted_orphan']}")

    if dry_run:
        print("\n[DRY RUN] No changes made. Run without --dry-run to execute.")
        return

    # 实际执行
    print("\n[Executing] Updating instructions.json files...")
    for json_path, kept, removed in to_update_instructions:
        print(f"  Updating {_rel_path(json_path)}")
        save_instructions(json_path, kept)

        # 同时更新 instruction.txt (legacy)
        txt_path = json_path.parent / "instruction.txt"
        if txt_path.exists() and kept:
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(get_instruction_text(kept[-1]))  # 最后一条

    print("\n[Executing] Deleting incomplete/orphan edit directories...")
    for edit_dir, reason in to_delete_edits:
        print(f"  Deleting {_rel_path(edit_dir)} ({reason})")
        shutil.rmtree(edit_dir)

    print("\n[Done] Cleanup completed.")


def main():
    parser = argparse.ArgumentParser(
        description="Cleanup excess instructions and incomplete edits"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without actually deleting anything",
    )
    args = parser.parse_args()

    cleanup(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
