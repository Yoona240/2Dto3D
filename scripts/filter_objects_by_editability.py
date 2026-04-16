#!/usr/bin/env python3
"""
Object Editability Filter

Filters categorized_objects_final.json by using GPT-5 to assess:
1. Editability: whether each object has enough distinguishable parts for 4-5 distinct edits
2. 3D generation friendliness: whether the object has attributes that cause poor 3D generation

Usage:
    python scripts/filter_objects_by_editability.py assess
    python scripts/filter_objects_by_editability.py assess --category "Food & Beverage"
    python scripts/filter_objects_by_editability.py review
    python scripts/filter_objects_by_editability.py apply
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# Project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.config import load_config
from utils.llm_client import get_llm_client

# Paths (defaults, can be overridden by --input)
DATA_DIR = PROJECT_ROOT / "data" / "captions"
DEFAULT_INPUT = DATA_DIR / "categorized_objects_final.json"


def _resolve_paths(input_file: Path):
    """Derive assessment and output paths from input file name."""
    stem = input_file.stem  # e.g. "categorized_objects_final" or "categorized_objects"
    parent = input_file.parent
    assessment = parent / f"{stem}_assessment.json"
    output = parent / f"{stem}_filtered.json"
    return assessment, output

BATCH_SIZE = 30
MAX_WORKERS = 5  # concurrent API calls

SYSTEM_PROMPT = """\
You are evaluating whether 3D objects are suitable for a 3D model editing dataset.
The dataset requires objects that can support multiple distinct editing operations (remove a part, replace a component, add a feature, change a sub-part's shape/color).

For each object, assess TWO things:

1. **Editability** (score 1-5): How many distinct, obvious editing operations can this object support?
   - Score 5: 5+ clearly distinguishable parts that can each be individually edited (e.g., truck: wheels, cabin, exhaust, headlights, bumper)
   - Score 4: 4 distinguishable editable parts
   - Score 3: 3 distinguishable editable parts (borderline)
   - Score 2: Only 1-2 possible edits, object is too simple
   - Score 1: Nearly no editable parts, pure geometric shape or amorphous

2. **3D Generation Friendliness**: Flag ONLY if the object's PRIMARY BODY / MAJORITY of its surface is problematic for mesh-based 3D generation. Do NOT flag objects that merely have a small transparent/reflective component.

   Flag as 3D-unfriendly ONLY for:
   - The object IS ENTIRELY or MOSTLY transparent/translucent: e.g., a glass cup, a clear plastic bag, a transparent raincoat. Do NOT flag objects that just have a small lens, screen, or window (cameras, TVs, microwaves, lamps are fine).
   - The object IS ENTIRELY a liquid, fluid, or pourable substance: e.g., shampoo (the liquid itself), soup, sauce. Do NOT flag the container (shampoo bottle is fine).
   - The object IS ENTIRELY a flat 2D sheet with no 3D structure: e.g., a sticker, a postcard, a sheet of paper. Do NOT flag clothing (clothes are generated as 3D garments with volume, not flat fabric), hats, bags, or any wearable item.
   - The object is amorphous/shapeless with no defined form: e.g., powder, crumbs, grits, whipped cream.
   - The object is too small to be a standalone 3D model: e.g., a single bead, a single thread, a single pea.

   Do NOT flag as 3D-unfriendly:
   - Clothing/garments (they are generated as 3D shapes with volume, NOT flat fabric)
   - Objects with small transparent parts (camera lens, lamp shade, TV screen, microwave door)
   - Objects with some reflective metal parts (trophy, crown, faucet, chandelier)
   - Umbrellas, hats, bags, shoes (these have clear 3D structure)
   - Lamps, lanterns, chandeliers (lamp shades can be opaque in 3D)

Important guidelines:
- Consider the object as a STANDALONE 3D model, not as part of a scene
- "Simple" means the object lacks distinguishable sub-parts, NOT that it's a common object
- A cup is simple (just a cylinder + handle), but a motorcycle is complex (wheels, engine, seat, mirrors, exhaust)
- Animals generally have enough parts (head, body, legs, tail, ears) - score them fairly
- Food items are usually simple unless they have clear structural components (e.g., hamburger has bun, patty, lettuce, cheese)
- Clothing items (dress, coat, jacket, shirt, etc.) should NOT be marked as 3D-unfriendly. They are generated as volumetric 3D garments.

Respond with a JSON array. Each element:
{
  "object": "<object name>",
  "editability_score": <1-5>,
  "edit_examples": ["<edit1>", "<edit2>", ...],
  "has_3d_unfriendly_attrs": <true/false>,
  "unfriendly_reasons": ["<reason1>", ...],
  "verdict": "<keep/remove>"
}

verdict rules:
- editability_score < 3 → "remove"
- has_3d_unfriendly_attrs == true → "remove"
- otherwise → "keep"

Return ONLY the JSON array, no markdown fences, no extra text."""


def _parse_llm_response(response_text: str) -> list[dict]:
    """Parse LLM JSON response, handling markdown fences."""
    text = response_text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return json.loads(text)


def _assess_batch(client, objects: list[str]) -> list[dict]:
    """Assess a batch of objects via LLM."""
    user_prompt = f"Objects to evaluate:\n{json.dumps(objects, ensure_ascii=False)}"
    response = client.chat(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=40000,
    )
    return _parse_llm_response(response)


def cmd_assess(args):
    """Run LLM assessment on all objects."""
    input_file = Path(args.input)
    assessment_file, _ = _resolve_paths(input_file)

    config = load_config()
    client = get_llm_client(config.qh_mllm)

    with open(input_file, "r", encoding="utf-8") as f:
        all_objects = json.load(f)

    # Load existing assessment (for resume)
    existing = {}
    if assessment_file.exists():
        with open(assessment_file, "r", encoding="utf-8") as f:
            existing = json.load(f)

    categories = [args.category] if args.category else list(all_objects.keys())

    # Validate category
    for cat in categories:
        if cat not in all_objects:
            print(f"ERROR: Category '{cat}' not found. Available: {list(all_objects.keys())}")
            sys.exit(1)

    # Build all (category, batch) pairs
    all_batches: list[tuple[str, list[str]]] = []
    for category in categories:
        objects = all_objects[category]
        assessed = set(existing.get(category, {}).keys()) if not args.force else set()
        remaining = [o for o in objects if o not in assessed]
        if not remaining:
            print(f"  [{category}] All {len(objects)} objects already assessed, skipping")
            continue
        for i in range(0, len(remaining), BATCH_SIZE):
            batch = remaining[i : i + BATCH_SIZE]
            all_batches.append((category, batch))

    total_batches = len(all_batches)
    if total_batches == 0:
        print("Nothing to assess. Use --force to re-assess.")
        return

    total_objects = sum(len(b) for _, b in all_batches)
    print(f"\nAssessing {total_objects} objects in {total_batches} batches "
          f"(batch_size={BATCH_SIZE}, workers={MAX_WORKERS})")
    print(f"Estimated time: {total_batches * 12 // MAX_WORKERS // 60}~{total_batches * 18 // MAX_WORKERS // 60} minutes\n")

    completed = 0
    failed = 0
    t0 = time.time()

    def _process_batch(cat_batch):
        cat, batch = cat_batch
        return cat, batch, _assess_batch(client, batch)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_process_batch, cb): cb for cb in all_batches}
        for future in as_completed(futures):
            cat, batch = futures[future][:2]
            try:
                cat_result, _, assessments = future.result()
                if cat_result not in existing:
                    existing[cat_result] = {}
                for item in assessments:
                    existing[cat_result][item["object"]] = item
                completed += 1
                elapsed = time.time() - t0
                rate = elapsed / completed
                remaining_time = rate * (total_batches - completed)
                print(f"  [{completed}/{total_batches}] {cat_result}: "
                      f"{len(batch)} objects assessed "
                      f"(ETA: {remaining_time:.0f}s)")
                # Save after each batch for resume
                with open(assessment_file, "w", encoding="utf-8") as f:
                    json.dump(existing, f, ensure_ascii=False, indent=2)
            except Exception as e:
                failed += 1
                completed += 1
                cat, batch = futures[future][:2]
                print(f"  [FAILED] {cat}: {len(batch)} objects - {e}")

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s. "
          f"Batches: {total_batches - failed} succeeded, {failed} failed.")
    print(f"Assessment saved to: {assessment_file}")


def cmd_review(args):
    """Review assessment results."""
    input_file = Path(args.input)
    assessment_file, _ = _resolve_paths(input_file)

    if not assessment_file.exists():
        print(f"No assessment file found at {assessment_file}. Run 'assess' first.")
        sys.exit(1)

    with open(assessment_file, "r", encoding="utf-8") as f:
        assessment = json.load(f)

    total_keep = 0
    total_remove = 0
    total = 0

    print("=" * 80)
    print("OBJECT EDITABILITY ASSESSMENT REVIEW")
    print("=" * 80)

    for category in sorted(assessment.keys()):
        items = assessment[category]
        keep = [k for k, v in items.items() if v.get("verdict") == "keep"]
        remove = [k for k, v in items.items() if v.get("verdict") == "remove"]
        total += len(items)
        total_keep += len(keep)
        total_remove += len(remove)

        print(f"\n{'─' * 60}")
        print(f"  {category}: {len(keep)} keep / {len(remove)} remove (total {len(items)})")
        print(f"{'─' * 60}")

        if remove:
            print("  REMOVE:")
            for obj_name in sorted(remove):
                item = items[obj_name]
                score = item.get("editability_score", "?")
                unfriendly = item.get("has_3d_unfriendly_attrs", False)
                reasons = item.get("unfriendly_reasons", [])
                reason_str = f" | 3D unfriendly: {', '.join(reasons)}" if unfriendly else ""
                print(f"    - {obj_name} (score={score}{reason_str})")

    print(f"\n{'=' * 80}")
    print(f"SUMMARY: {total_keep} keep / {total_remove} remove / {total} total")
    print(f"Keep rate: {total_keep / total * 100:.1f}%")
    print(f"{'=' * 80}")


def cmd_apply(args):
    """Apply filter and generate the filtered JSON."""
    input_file = Path(args.input)
    assessment_file, output_file = _resolve_paths(input_file)

    if not assessment_file.exists():
        print(f"No assessment file found at {assessment_file}. Run 'assess' first.")
        sys.exit(1)

    with open(input_file, "r", encoding="utf-8") as f:
        all_objects = json.load(f)

    with open(assessment_file, "r", encoding="utf-8") as f:
        assessment = json.load(f)

    filtered = {}
    total_before = 0
    total_after = 0

    for category, objects in all_objects.items():
        total_before += len(objects)
        cat_assessment = assessment.get(category, {})

        kept = []
        for obj in objects:
            item = cat_assessment.get(obj)
            if item is None:
                # Not assessed — keep by default (shouldn't happen if assess ran fully)
                print(f"  WARNING: '{obj}' in '{category}' not assessed, keeping by default")
                kept.append(obj)
            elif item.get("verdict") == "keep":
                kept.append(obj)

        if kept:
            filtered[category] = kept
            total_after += len(kept)
            print(f"  {category}: {len(objects)} → {len(kept)}")
        else:
            print(f"  {category}: {len(objects)} → 0 (category removed)")

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(filtered, f, ensure_ascii=False, indent=2)

    print(f"\nTotal: {total_before} → {total_after} "
          f"({total_after / total_before * 100:.1f}% kept)")
    print(f"Output: {output_file}")


def main():
    parser = argparse.ArgumentParser(description="Filter objects by editability")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Common argument for all subcommands
    parser.add_argument("--input", type=str, default=str(DEFAULT_INPUT),
                        help="Input JSON file (default: categorized_objects_final.json)")

    # assess
    p_assess = subparsers.add_parser("assess", help="Run LLM editability assessment")
    p_assess.add_argument("--category", type=str, help="Only assess a specific category")
    p_assess.add_argument("--force", action="store_true", help="Re-assess even if already done")
    p_assess.set_defaults(func=cmd_assess)

    # review
    p_review = subparsers.add_parser("review", help="Review assessment results")
    p_review.set_defaults(func=cmd_review)

    # apply
    p_apply = subparsers.add_parser("apply", help="Apply filter and generate filtered JSON")
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
