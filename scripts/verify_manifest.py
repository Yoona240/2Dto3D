#!/usr/bin/env python3
"""
Verify manifest.json: check all files exist on server and instruction matches edit meta.json.
"""
import os
import json
import re

VIEWS = ["front", "back", "left", "right", "top", "bottom"]
ROOT = "/seaweedfs/xiaoliang/data/2d3d_data"

manifest_path = "/tmp/manifest_to_verify.json"
with open(manifest_path) as f:
    manifest = json.load(f)

results = []

for s in manifest["samples"]:
    sid = s["sample_id"]
    issues = []
    info = []

    # 1. source_glb exists
    src_glb = s["source_glb"]
    if not os.path.exists(src_glb):
        issues.append(f"MISSING source_glb: {src_glb}")
    else:
        info.append("source_glb OK")

    # 2. target_glb exists and is different from source_glb
    tgt_glb = s["target_glb"]
    if not os.path.exists(tgt_glb):
        issues.append(f"MISSING target_glb: {tgt_glb}")
    else:
        info.append("target_glb OK")
    if src_glb == tgt_glb:
        issues.append("WARN source_glb == target_glb (same file!)")

    # 3. source_views all exist
    missing_src_views = [v for v in VIEWS if not os.path.exists(s["source_views"][v])]
    if missing_src_views:
        issues.append(f"MISSING source_views: {missing_src_views}")
    else:
        info.append("source_views(6) OK")

    # 4. edited_views all exist
    missing_edit_views = [v for v in VIEWS if not os.path.exists(s["edited_views"][v])]
    if missing_edit_views:
        issues.append(f"MISSING edited_views: {missing_edit_views}")
    else:
        info.append("edited_views(6) OK")

    # 5. instruction consistency: derive edit_id from edited_views path
    # path: .../triplets/<model_id>/edited/<edit_id>/front.png
    edit_view_front = s["edited_views"]["front"]
    # extract model_id and edit_id
    m = re.search(r"triplets/([^/]+)/edited/([^/]+)/front\.png", edit_view_front)
    if m:
        model_id_from_path = m.group(1)
        edit_id_from_path = m.group(2)
        meta_path = f"{ROOT}/triplets/{model_id_from_path}/edited/{edit_id_from_path}/meta.json"
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                em = json.load(f)
            server_instruction = em.get("instruction", "").strip()
            manifest_instruction = s["instruction"].strip()
            if server_instruction == manifest_instruction:
                info.append("instruction MATCH")
            else:
                issues.append(f"INSTRUCTION MISMATCH")
                issues.append(f"  manifest : {manifest_instruction!r}")
                issues.append(f"  server   : {server_instruction!r}")
        else:
            issues.append(f"MISSING edit meta.json: {meta_path}")
    else:
        issues.append(f"Cannot parse edit_id from path: {edit_view_front}")

    # 6. check instruction typo hint
    if s["instruction"].startswith("RR") or "  " in s["instruction"]:
        issues.append(f"WARN possible typo in instruction: {s['instruction'][:60]!r}")

    results.append((sid, issues, info))

# Print report
print("=" * 70)
print("VERIFICATION REPORT")
print("=" * 70)
all_ok = True
for sid, issues, info in results:
    status = "OK" if not issues else "ISSUES"
    print(f"\n[{sid}] {status}")
    for i in info:
        print(f"  + {i}")
    for i in issues:
        print(f"  ! {i}")
    if issues:
        all_ok = False

print("\n" + "=" * 70)
if all_ok:
    print("ALL SAMPLES PASS")
else:
    fail_count = sum(1 for _, issues, _ in results if issues)
    print(f"FAILED: {fail_count}/{len(results)} samples have issues")
print("=" * 70)
