# core/tools/image_processor.py

from typing import Optional, Tuple, Dict
from pathlib import Path
from PIL import Image, ImageChops
import os


class ImageProcessor:
    """
    ImageProcessor 2.0
    支持三种裁剪策略：
    1) foreground（适用于 tripo / hunyuan）
    2) seed_canvas_fix（完全复刻你提供的 seed 专用逻辑）
    3) none（不裁剪）

    所有参数全部由 preset 配置文件控制：
    preset["image_processing"]["crop"]
    """

    # ----------------------------------------------------------------------
    # 外部调用主接口
    # ----------------------------------------------------------------------
    def process(self, image_path: str, crop_cfg: Dict):
        """
        根据 preset.crop.strategy 自动选择裁剪策略
        """

        if not crop_cfg.get("enabled", False):
            return image_path  # 不裁剪

        strategy = crop_cfg.get("strategy", "foreground")

        if strategy == "foreground":
            return self._process_foreground(image_path, crop_cfg)

        elif strategy == "seed_canvas_fix":
            return self._process_seed_fix(image_path, crop_cfg)

        elif strategy == "none":
            return image_path

        else:
            print(f"❌ 未知裁剪策略: {strategy}")
            return image_path

    # ======================================================================
    #  1) Foreground Crop（用于 Hunyuan / Tripo）
    # ======================================================================

    def _process_foreground(self, image_path: str, crop_cfg: Dict) -> str:
        """
        1. 前景裁剪（检测白底）
        2. 保证最小尺寸 min_side
        3. 轻度宽高比控制：保持 0.30 ~ 3.0（可通过 preset 配置）
        """

        img = Image.open(image_path).convert("RGB")
        w, h = img.size

        # --- 1. 前景区域检测 ---
        bbox = self._find_foreground_bbox(img, crop_cfg.get("tolerance", 15))
        if bbox:
            img = img.crop(bbox)

        # --- 2. 轻度宽高比控制 ---
        aspect_cfg = crop_cfg.get("aspect_control", {})
        if aspect_cfg.get("enabled", True):
            img = self._apply_aspect_light(img,
                                           aspect_cfg.get("min_aspect", 0.30),
                                           aspect_cfg.get("max_aspect", 3.00))

        # --- 3. 保证最小尺寸 ---
        min_side = crop_cfg.get("min_side", 128)
        img = self._ensure_min_size(img, min_side)

        # --- 保存 ---
        save_path = self._save_processed_image(image_path, img, crop_cfg)
        return save_path

    # ======================================================================
    #  前景检测
    # ======================================================================
    def _find_foreground_bbox(self, img: Image.Image, tolerance: int = 15):
        """
        检测非白区域的 bounding box
        """
        rgb = img.convert("RGB")
        white = Image.new("RGB", rgb.size, (255, 255, 255))
        diff = ImageChops.difference(rgb, white)
        gray = diff.convert("L")
        mask = gray.point(lambda v: 255 if v > tolerance else 0, mode="1")
        return mask.getbbox()

    # ======================================================================
    # 轻度宽高比控制（Tripo/Hunyuan）
    # ======================================================================
    def _apply_aspect_light(self, img: Image.Image, min_aspect: float, max_aspect: float):
        w, h = img.size
        aspect = w / h

        # 在范围内无需调整
        if min_aspect <= aspect <= max_aspect:
            return img

        # 创建透明背景
        if aspect < min_aspect:
            # 窄图 → 补宽
            target_w = int(h * min_aspect)
            target_h = h
        else:
            # 宽图 → 补高
            target_h = int(w / max_aspect)
            target_w = w

        canvas = Image.new("RGB", (target_w, target_h), (255, 255, 255))
        offset_x = (target_w - w) // 2
        offset_y = (target_h - h) // 2
        canvas.paste(img, (offset_x, offset_y))

        return canvas

    # ======================================================================
    #  保证最小尺寸（常用于 Tripo/Hunyuan）
    # ======================================================================
    def _ensure_min_size(self, img: Image.Image, min_side: int):
        w, h = img.size
        new_w = max(w, min_side)
        new_h = max(h, min_side)

        # 不需要扩展
        if new_w == w and new_h == h:
            return img

        canvas = Image.new("RGB", (new_w, new_h), (255, 255, 255))
        canvas.paste(img, ((new_w - w) // 2, (new_h - h) // 2))
        return canvas

    # ======================================================================
    #  2) Seed 专用严格校准策略（你提供的逻辑）
    # ======================================================================
    def _process_seed_fix(self, image_path: str, crop_cfg: Dict) -> str:
        """
        完整复刻你的 seed 修复脚本逻辑：
        - 高度 < MIN_HEIGHT → 增高
        - 宽高比过小 → 补宽
        - 宽高比 > MAX_ASPECT → 补高
        """
        img = Image.open(image_path).convert("RGBA")
        w, h = img.size

        MIN_HEIGHT = crop_cfg.get("min_height", 300)
        MIN_ASPECT = crop_cfg.get("min_aspect", 0.40)
        MAX_ASPECT = crop_cfg.get("max_aspect", 2.50)
        SAFE_ASPECT = MAX_ASPECT - 0.01  # 安全区间

        target_w, target_h = w, h

        # 1. 高度不足
        if h < MIN_HEIGHT:
            target_h = MIN_HEIGHT

        new_aspect = target_w / target_h

        # 2. 过窄（宽高比 < 0.4）
        if new_aspect < MIN_ASPECT:
            target_w = int(target_h * MIN_ASPECT)

        # 3. 过宽（宽高比 >= 2.50）
        elif new_aspect >= MAX_ASPECT:
            target_h = int(target_w / SAFE_ASPECT)

        # 无需修改
        if target_w == w and target_h == h:
            return image_path

        canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))

        offset_x = (target_w - w) // 2
        offset_y = (target_h - h) // 2
        canvas.paste(img, (offset_x, offset_y), img)

        save_path = self._save_processed_image(image_path, canvas, crop_cfg)
        return save_path

    # ======================================================================
    # 保存裁剪后的图片（覆盖/输出到目录皆可）
    # ======================================================================
    def _save_processed_image(self, original_path: str, img: Image.Image, crop_cfg: Dict):
        output_dir = crop_cfg.get("output_dir")

        if output_dir:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            save_path = Path(output_dir) / Path(original_path).name
        else:
            save_path = original_path  # 覆盖原图

        # 自动根据 PNG/非 PNG 保存格式
        ext = original_path.lower()
        if ext.endswith(".png"):
            img.save(save_path, "PNG")
        else:
            img.save(save_path, "JPEG")

        return str(save_path)
