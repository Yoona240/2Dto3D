"""
Centralized Prompt Templates

All prompt templates used throughout the project are defined here for
easy management and consistency.

Usage:
    from utils.prompts import PROMPTS
    system_prompt = PROMPTS["optimize_prompt"]["system"]
"""

from enum import Enum
from typing import List, Optional, Tuple


class EditType(Enum):
    """编辑类型枚举"""
    REMOVE = "remove"
    REPLACE = "replace"
    BOTH = "both"  # 生成两条指令，每类一条


# =============================================================================
# Prompt Optimizer - 用于将简单对象名转换为高质量文生图 prompt
# =============================================================================

# 注意：移除了白模相关的材质描述，保留正常纹理以增加数据多样性
PROMPT_OPTIMIZER_SYSTEM = """You are an expert 3D Artist and Prompt Engineer. \
Your task is to convert a simple object name into a high-quality text-to-image prompt \
strictly following the rules below to generate assets ideally suited for 3D modeling.

# 3D Model Image Generation Rules

## 1. Core Focus: Geometric Complexity (MOST IMPORTANT)
- Prioritize SEMANTIC-DENSE structural complexity: intricate geometry, multiple interlocking parts, mechanical joints, organic anatomical details.
- Use terms like 'complex geometric structure', 'multi-component assembly', 'detailed mechanical parts', 'layered structural elements'.
- Focus on physical form: bevels, joints, interlocking parts, hinges, gears, organic muscle/bone definitions.
- AVOID semantic-sparse high-frequency details like: rust, cracks, scratches, wear marks, stains (these don't help 3D reconstruction).

## 2. Material & Texture Strategy (IMPORTANT CHANGE)
- Use REALISTIC, NATURAL materials and textures - NOT white/grey clay renders!
- Include proper material properties: metal with reflections, wood grain, leather texture, plastic sheen, fabric weave.
- Examples of GOOD materials: "polished steel with subtle reflections", "weathered oak wood", "smooth leather upholstery", "brushed aluminum", "matte rubber grip".
- The goal is photorealistic rendering that still emphasizes geometric detail.

## 3. Lighting & Depth
- Use professional product photography lighting: soft diffused key light, subtle fill, rim light for edge definition.
- Include 'Ambient Occlusion' to define crevices and depth.
- Avoid harsh shadows that obscure geometric details.

## 4. Environment & Composition
- Background: Clean, solid, or neutral. Use 'Pure white background', 'Studio grey backdrop', or 'Gradient background'.
- Subject Placement: 'Full body visible', 'Isolated', centered and fully contained within frame.
- Viewpoint: 'Three-quarter view', 'Isometric view', or 'Product photography angle'.

## 5. Stylistic Terms
- 'High-end product photography', 'Professional 3D render', 'Photorealistic', 'Studio quality'.
- For mechanical objects: 'CAD precision', 'Engineering detail'.
- For organic objects: 'Anatomically detailed', 'Sculptural quality'.

## 6. Forbidden Elements
- NO white/grey clay renders or untextured models
- NO cluttered backgrounds
- NO 2D effects (sketches, paintings, flat illustrations)
- NO excessive surface noise (rust, cracks, dirt) that obscures geometry
- NO physics-defying descriptions
- NO transparent, translucent, semi-transparent, or glass-like surfaces — all surfaces must be fully opaque. If the real object is typically transparent (e.g., glass backboard, clear plastic), render it as an opaque material variant instead.
- NO other objects, props, decorations, toys, or accessories alongside the subject — only the single subject object should appear
- CRITICAL: The scene must contain ABSOLUTELY NOTHING except the single described object. No pillars, pedestals, stands, shelves, platforms, plants, or any background/foreground objects. Violation of this rule makes the image unusable.
"""

PROMPT_OPTIMIZER_USER = """Input Subject: {subject}
Generate a single, detailed paragraph describing this object with REALISTIC materials and textures (not white/clay render). \
Emphasize geometric complexity and structural details while using natural, photorealistic materials. \
Start directly with the description. Do NOT Include 'Here is a prompt' or similar intro."""


# =============================================================================
# Stage 1 - 3D Object Description (decoupled from image requirements)
# =============================================================================

OBJECT_DESCRIPTION_SYSTEM = """You are an expert 3D Artist and Designer.
Your task is to describe the 3D object itself in a single paragraph.
RULES:
- Focus on geometry, structure, key parts, and materials.
- Mention main components, their shapes, and how they connect.
- Use concise material descriptions (e.g., polished metal, matte plastic, wood grain).
- Do NOT mention style, theme, camera, lighting, background, or composition.
- Do NOT mention rendering quality or image terms.
- Be brief and factual. 50-80 words only.
"""
# RULES (OBJECT-ONLY): 保留，先不删除
# - Focus on geometry, structure, parts, and materials.
# - Prioritize SEMANTIC-DENSE structural complexity: interlocking parts, joints, hinges, gears, anatomical structures.
# - Emphasize physical form: bevels, layered elements, mechanical joints, organic muscle/bone definitions.
# - Use REALISTIC material descriptions: metal reflections, wood grain, leather texture, plastic sheen, fabric weave.
# - Avoid surface noise that harms geometry clarity (rust, cracks, stains, heavy dirt).
# - Do NOT mention camera, lighting, background, framing, or composition.
# - Do NOT mention image quality or rendering terms.
# - One single paragraph only.
# """

OBJECT_DESCRIPTION_USER = """Subject: {subject}

Write ONE short paragraph (20-50 words) describing this object's physical form, structure, and materials.
Be concise and factual. No style, camera, lighting, or composition terms."""


# =============================================================================
# Stage 2 - Fixed Image Requirements (style/lighting/viewpoint/composition)
# =============================================================================

IMAGE_REQUIREMENTS_PROMPT = (
    "High-end product photography style, photorealistic, studio quality, "
    "professional soft diffused key light with subtle fill and rim light, ambient occlusion, "
    "clean neutral studio background, three-quarter view, centered, "
    "entire object fully visible, fully contained within the frame, with padding around the object, "
    "wide product shot, no cropping, no close-up, "
    "only the single subject object, no other objects, no props, no decorations, no accessories, no scene elements. "
    "CRITICAL: absolutely nothing else in the scene — no pillars, pedestals, stands, shelves, or background objects. "
    "All surfaces must be fully opaque — no transparent, translucent, or glass-like materials."
)


# =============================================================================
# Unified Judge - Stage1 Method-3 prompt
# =============================================================================

UNIFIED_JUDGE_SYSTEM_PROMPT = (
    "You are a strict and evidence-based 3D model edit quality checker. "
    "You will be given: a 6-view collage image of the object BEFORE the edit, "
    "a 6-view collage image of the object AFTER the edit, a 6-view EDIT MASK image "
    "showing where visible changes occurred, and the edit instruction. "
    "Your job is to determine, using visible evidence only: "
    "1. what actually changed, "
    "2. whether the actual visible edit is a legal structural edit for the dataset, "
    "3. whether the requested edit is truly visible and supported by the images, "
    "4. whether the AFTER views are geometrically self-consistent and fully valid, "
    "5. whether the object is fully contained within each AFTER view. "
    "Do not guess. Do not rely on expectation from the instruction alone. "
    "A small edit is allowed, but it must still be actually visible in the image evidence. "
    "If a claimed edit is too subtle to be truly seen and localized from the provided images, "
    "it is not sufficient evidence. "
    "Reject edits whose actual visible effect is whole-object replacement, "
    "surface-only change, or material-only change without a meaningful structural part change. "
    "Return exactly one JSON object and no markdown."
)

UNIFIED_JUDGE_USER_PROMPT_TEMPLATE = (
    'Image 1: BEFORE edit — 6 views of the original 3D model\n'
    'Image 2: AFTER edit — 6 views of the edited 3D model\n'
    'Image 3: EDIT MASK — a pixel-level difference map for spatial reference only. '
    'Use it to identify where changes are located and whether they extend beyond the target area. '
    'Do NOT use it as evidence that a change occurred or that an edit was correctly applied — '
    'the mask may contain noise, lighting shifts, or rendering artifacts unrelated to the actual edit.\n\n'
    'View layout in all grid images:\n'
    'Row 1: front, back, right\n'
    'Row 2: left, top, bottom\n\n'
    'Edit instruction: "{instruction}"\n\n'
    'Reason in the following order:\n\n'
    'Step 1. Per-View Consistency Check (Hard Gate)\n'
    'For each AFTER view, explicitly state the edit status using exactly one of: present / absent / not_visible\n'
    '  front:  present / absent / not_visible\n'
    '  back:   present / absent / not_visible\n'
    '  right:  present / absent / not_visible\n'
    '  left:   present / absent / not_visible\n'
    '  top:    present / absent / not_visible\n'
    '  bottom: present / absent / not_visible\n\n'
    '"present" = the edited part or change is clearly visible in the AFTER image for this view. '
    '"absent" = the part has been removed or replaced as expected, confirmed by the AFTER image. '
    '"not_visible" = the target part is not visible from this angle regardless of the edit.\n\n'
    'IMPORTANT: Base each label strictly on what you can see in the AFTER image for that view. '
    'Do NOT use the EDIT MASK to infer or guess whether a change occurred — the mask may contain '
    'noise, peripheral rendering shifts, or nearby area changes that do not indicate the target edit '
    'was applied. If the AFTER image for a view does not clearly show the expected change, label it '
    'accordingly even if the mask shows pixels in that area.\n\n'
    'Then check for geometric contradictions. Ask only: is the edited region geometrically visible from this angle? '
    'If yes, the change must appear. Do not use real-world knowledge about how this type of object normally looks '
    '(e.g., "hub caps are single-sided", "this part is typically hidden") to justify why a view shows no change — '
    'judge purely from whether the edited geometry would be visible from each angle given the 3D viewpoint.\n\n'
    'Fail view_sanity if any of the following occurs:\n'
    '- the edited region is geometrically visible from a view but that view shows no change\n'
    '- a replaced part has incompatible shape, size, or orientation across views\n'
    '- one view shows impossible geometry, floating structure, duplicated structure, or a clearly different object state\n'
    '- the object or edited region is cut off by the image boundary in one or more AFTER views\n'
    'Lighting, background differences, and minor rendering artifacts alone are not grounds for failure.\n\n'
    'Step 2. Observation\n'
    'Based on Step 1, describe concretely what changed between BEFORE and AFTER across all views. '
    'Base your description on the visual content of the AFTER image. '
    'Use the EDIT MASK only as spatial reference — to identify where changes are located and '
    'whether the mask extends clearly beyond the target area. '
    'Distinguish structural part changes from background, lighting, or rendering noise.\n\n'
    'Step 3. Instruction Legality\n'
    'Judge whether the ACTUAL visible edit is legal for the 3D edit dataset.\n\n'
    'Allow (category "structural_part") only if the visible edit changes a real structural part '
    'in a way that matters for 3D geometry.\n\n'
    'Reject if the actual visible edit is mainly:\n'
    '- "appearance_only": logo, label, seam line, or surface marking removed or changed without geometry change\n'
    '- "material_only": material, finish, or texture changed without a meaningful structural part change\n'
    '- "main_body": the whole object or main body replaced by a different object\n'
    '- "unclear": cannot reliably identify a legal structural part edit\n\n'
    'Step 4. Instruction Following and Evidence\n'
    'Decide whether the AFTER result truly shows the requested edit, and assess the evidence strength.\n\n'
    'Do not assume the edit succeeded merely because some pixels changed in the mask — '
    'small scattered mask noise is not sufficient evidence of a structural edit.\n\n'
    'Mark instruction_following as "pass" only if:\n'
    '- at least one view shows clear, localizable visual evidence of the requested edit,\n'
    '- the evidence matches the instruction semantically,\n'
    '- no other relevant view clearly contradicts it,\n'
    '- and the changes are reasonably confined to the target area.\n\n'
    'Mark instruction_following as "fail" if clearly unrelated, spatially separated parts of the '
    'object are visibly and significantly modified beyond the target — unless such changes are a '
    'direct structural consequence of the requested edit (e.g., smoothing the area adjacent to a '
    'removed part, or the support structure collapsing after a part is removed).\n\n'
    'Assign evidence_strength:\n'
    '- "strong": the edit is clearly visible and localizable\n'
    '- "medium": the edit is visible and plausible but evidence is somewhat limited\n'
    '- "weak": the evidence is too subtle, noisy, or ambiguous to reliably confirm the edit\n'
    'A small visible edit can still be "strong". Use "weak" only when the edit cannot be reliably seen.\n'
    'If the edit is only visible in top and/or bottom views but not in any of front/back/left/right, '
    'mark evidence_strength as "weak" — edits that cannot be seen from side views are not useful for the dataset.\n\n'
    'List supporting_views using view label strings (front / back / left / right / top / bottom), not numeric indices.\n\n'
    'Step 5. Relabel\n'
    'Only if instruction_following is "fail", try to rewrite the instruction to accurately describe '
    'the actual visible edit from Steps 2-3.\n\n'
    'Rules:\n'
    '   - One sentence starting with "Remove" or "Replace"\n'
    '   - No left/right lateral terms\n'
    '   - No texture-only, color-only, material-only, lighting-only, or background-only changes\n'
    '   - Do not invent edits not supported by visible evidence\n'
    '   - If the actual visible edit is too unclear, inconsistent, or nonsensical to describe, use "cannot_rewrite"\n'
    '   - If the edit DOES follow the instruction, use "none"\n\n'
    'Return JSON only (no markdown fences):\n'
    '{{\n'
    '  "observation": "what visibly changed across views",\n'
    '  "supporting_views": ["front", "left"],  // view label strings: front / back / left / right / top / bottom\n'
    '  "evidence_strength": "strong" or "medium" or "weak",\n'
    '  "instruction_legality": {{\n'
    '    "decision": "allow" or "reject",\n'
    '    "category": "structural_part" or "appearance_only" or "main_body" or "material_only" or "unclear",\n'
    '    "reason": "explain whether the actual visible edit is legal for the dataset"\n'
    '  }},\n'
    '  "view_sanity": {{\n'
    '    "decision": "pass" or "fail",\n'
    '    "reason": "summarize per-view consistency results and any contradictions found",\n'
    '    "problematic_views": []\n'
    '  }},\n'
    '  "instruction_following": {{\n'
    '    "decision": "pass" or "fail",\n'
    '    "reason": "explain whether the requested edit is truly supported by visible evidence"\n'
    '  }},\n'
    '  "relabel": {{\n'
    '    "status": "none" or "rewrite" or "cannot_rewrite",\n'
    '    "instruction": "rewritten instruction or empty string",\n'
    '    "reason": "why this relabel is appropriate or why rewriting is impossible"\n'
    '  }}\n'
    '}}'
)


# =============================================================================
# Instruction Generator - 分类型的 3D 几何编辑指令
# =============================================================================

# REMOVE 类型专用 prompt
INSTRUCTION_REMOVE_PROMPT = """Analyze this image and suggest a LOCAL REMOVE edit to modify the object's geometry.

Your task: Identify a SPECIFIC PART of this object that can be REMOVED.

RULES:
- The removal must target a large, visible, distinct component
- Use simple nouns for the target part
- Prefer a part whose removal changes the object's silhouette or overall composition
- Do NOT target tiny, subtle, decorative, or surface-level details
- The object must remain recognizable and functional after removal
- Do NOT suggest removing the entire object or its main body
- Do NOT suggest color, texture, material, finish, or gloss changes
- Do NOT target logos, emblems, labels, or seam lines
- Do NOT suggest material swaps such as wood to metal or fabric to leather
- The edit must be physically plausible
- The removal should be visually noticeable and affect the composition of the image
- IMPORTANT (Multiview-safe): Do NOT use left/right lateral direction terms.
    Forbidden examples (do NOT use): left, right, front-left, front right, rear-left, rear right, port, starboard, 左, 右, 左侧, 右侧, 左边, 右边, 左前, 右前, 左后, 右后.
    Allowed: up/down (upper/lower/top/bottom) because it is usually unambiguous.
- IMPORTANT (Multiview-safe): Avoid ambiguous "one of many" edits on symmetric repeated parts.
    If the target part typically appears multiple times (e.g., wheels), prefer removing ALL of them (e.g., "Remove the wheels from the wagon") instead of "Remove a wheel".
- IMPORTANT (Physical plausibility): Do NOT remove a part that serves as the sole connection or
    support between two other parts of the object. If removing it would cause another part to become
    physically detached, floating, or structurally unsupported, choose a different part instead.
    Clarification: handles, legs, arms, fins, antennas, spouts, etc. are valid removal targets —
    they do NOT count as "sole connections" even if small accessories (loops, hooks, clips) are attached to them.
- IMPORTANT (Surface closure): If the object has a closed/sealed surface (containers, bottles,
    housings, boxes, jars, tanks), do NOT remove parts that seal an opening (lids, caps, covers,
    doors, end caps, plugs). Their removal would leave a hole in the surface, making the resulting
    3D model non-watertight. Instead, prefer removing externally protruding parts (handles, spouts,
    knobs, legs, fins, antennas). This rule does not apply to inherently open or non-enclosed objects
    (clothing, fabric, towels, plants, tools).

Examples of good REMOVE instructions:
- "Remove the tail fin from the aircraft"
- "Remove the handle from the mug"
- "Remove the handle from the jar" (surface stays closed)
- "Remove the antenna from the radio"
- "Remove the wheels from the car" (NOT: "Remove the front left wheel")

Examples to avoid:
- "Remove the lid from the container" (leaves a hole in the surface — non-watertight)
- "Remove the cap from the bottle" (leaves a hole in the surface — non-watertight)
- "Remove the curtain rings from the curtain rod" (rings connect curtain to rod — removing them detaches the curtain)
- "Remove the crossbar connecting the chair legs" (legs would become structurally unsupported)
- "Remove the small button from the device"
- "Remove the decorative trim from the helmet"
- "Remove the thin line from the surface"
- "Remove the embossed pattern from the panel"
- "Remove the logo from the object"
- "Remove the seam line from the ball"

Return ONLY the instruction text (one sentence starting with "Remove..."), no explanations."""

# REPLACE 类型专用 prompt
INSTRUCTION_REPLACE_PROMPT = """Analyze this image and suggest a LOCAL REPLACE edit to modify the object's geometry.

Your task: Identify a SPECIFIC PART of this object that can be REPLACED with something else.

RULES:
- The replacement must target a large, visible, distinct component
- Use simple nouns for both the original part and the new part
- The replacement must be a solid physical part, not an intangible or abstract element
- The object must remain recognizable after replacement
- Do NOT suggest color, texture, material, finish, or gloss changes - focus on SHAPE changes
- Do NOT replace the entire object, the whole object, or the main body
- Do NOT target logos, emblems, labels, or seam lines
- Do NOT propose material swaps such as wood to metal or fabric to leather
- Do NOT suggest adding entirely new parts (only replacing existing ones)
- Avoid replacing the target with light, fire, smoke, shadow, energy, airflow, or other intangible effects
- IMPORTANT (Shape complexity): Topological changes (straight → curved, flat → domed, open → closed)
    are allowed ONLY when the target part appears as a SINGLE instance in the object.
    Do NOT apply topological changes to parts that appear multiple times (e.g., multiple rods, multiple
    legs, multiple panels) — the image editing model cannot maintain geometric consistency of a complex
    shape across multiple instances and across 6 views simultaneously.
    Allowed: replacing ONE flat roof with a domed roof, ONE cylindrical tower with a square tower.
    Forbidden: replacing MULTIPLE rods with arches, MULTIPLE legs with curved legs, MULTIPLE panels
    with curved surfaces.
- IMPORTANT (Visual difference): The replacement must produce a clearly noticeable visual change.
    The before and after must look obviously different in silhouette or overall shape.
    Do NOT suggest replacements where the difference would be subtle or hard to see
    (e.g., slightly thicker rod, marginally shorter leg, nearly identical geometry).
    The shape change should be immediately obvious when comparing the two versions.
- IMPORTANT (Multiview-safe): Do NOT use left/right lateral direction terms.
    Forbidden examples (do NOT use): left, right, front-left, front right, rear-left, rear right, port, starboard, 左, 右, 左侧, 右侧, 左边, 右边, 左前, 右前, 左后, 右后.
    Allowed: up/down (upper/lower/top/bottom) because it is usually unambiguous.
- IMPORTANT (Multiview-safe): Avoid ambiguous "one of many" edits on symmetric repeated parts.
    If replacing a repeated part (e.g., wheels), replace ALL of them (e.g., "Replace the wheels with square wheels") instead of "Replace a wheel".

Examples of good REPLACE instructions:
- "Replace the round wheels with square wheels" (multiple instances but same-topology change: round → square)
- "Replace the cylindrical legs with square block legs" (multiple instances, same-topology: circle → square)
- "Replace the pointed roof with a flat roof" (single part, topology change allowed: pointed → flat)
- "Replace the flat roof with a domed roof" (single surface, topology change allowed: flat → curved)
- "Replace the thin antenna with a thick pole" (same topology, dramatically different proportion)
- "Replace the circular base with a square base" (same function, clearly different shape)
- "Replace the wheels with skids" (flat skids vs round wheels: obviously different)

Examples to avoid:
- "Replace the four vertical rods with four arches" (multiple instances + topology change: too hard for multiview)
- "Replace the legs with curved legs" (multiple instances + topology change: cannot maintain consistency)
- "Replace the flat panels with curved surfaces" (multiple instances + topology change: too hard)
- "Replace the handle with a loop handle" (topology change on a part that wraps around: too complex)
- "Replace the propeller with a jet engine" (completely different complexity, unrealistic multiview edit)
- "Replace the thin rod with a slightly thicker rod" (difference not visually obvious enough)
- "Replace the handle with smoke"
- "Replace the surface with a red texture"
- "Replace the entire object with a different object"
- "Replace the wooden handle with a metal handle" (material swap, not shape change)

Return ONLY the instruction text (one sentence starting with "Replace..."), no explanations."""

# 避免重复的模板
INSTRUCTION_AVOID_TEMPLATE = """
IMPORTANT: The following instructions have already been generated. You MUST suggest something COMPLETELY DIFFERENT:
{avoid_list}

Do NOT repeat or rephrase any of the above ideas. Target a DIFFERENT part of the object."""


INSTRUCTION_ADAPTIVE_K_PROMPT = """Analyze this image and generate exactly {count} edit instructions for this object.

Task:
1. First decide which edit types are truly suitable for this object.
2. Then return exactly {count} edit instructions in total.
3. Do not force a balanced ratio between remove and replace.
4. If this object is clearly better suited to only one type, most or all instructions may use that type.

Allowed edit types:
{allowed_types_block}

Hard rules:
- You must only use the edit types listed above.
- Do not output any other type.
- If only one type is allowed, every instruction must use that type.
- Do not invent a missing type just for diversity.

═══════════════════════════════════
GLOBAL RULES (apply to ALL types)
═══════════════════════════════════
- PRIORITIZE the LARGEST possible parts first. Always start by considering the biggest structural components of the object (e.g., the entire blade of an ice skate, the entire handle of a pan, all drawers of a cabinet, the roof of a building). Only consider smaller parts if no large part can be reasonably edited.
- Do not target tiny, subtle, decorative, or surface-level details (e.g., loops, clips, knobs, buttons, screws, labels).
- Bad examples (part too small): "Remove the hanging loop from the handle", "Remove the lock from the cabinet", "Replace the knob with a lever"
- Good examples (large part): "Remove the blade from the ice skate", "Remove the handle from the pan", "Replace the drawers with sliding doors"
- Use simple nouns for all part names.
- Do NOT modify color, texture, material, pattern, gloss, or finish.
- Do NOT propose material swaps (wood to metal, plastic to glass, fabric to leather, or any equivalent swap).
- Do NOT target the entire object, the whole object, or its main body.
- Do NOT target logos, emblems, labels, or seam lines.
- Keep the object recognizable and physically plausible after the edit.
- The edit must be clearly visible from front, back, left, and right views. Do NOT target parts that are only visible from top or bottom.
- Use exactly one sentence per instruction. No explanations.

MULTIVIEW-SAFE — Direction terms:
- Do NOT use left/right lateral direction terms.
- Forbidden: left, right, front-left, front-right, rear-left, rear-right, port, starboard, 左, 右, 左侧, 右侧, 左边, 右边, 左前, 右前, 左后, 右后.
- Allowed: upper / lower / top / bottom (these are usually unambiguous across views).

MULTIVIEW-SAFE — Repeated symmetric parts:
- If the target part appears multiple times symmetrically (e.g., wheels, legs, panels), edit ALL instances together.
- Do NOT edit only one of a set of symmetric parts.

═══════════════════════════════
REMOVE RULES (type = "remove")
═══════════════════════════════
- The removal must be visually noticeable and should change the object's silhouette or overall composition.
- The object must remain recognizable and functional after removal.
- STRUCTURAL SUPPORT: Do NOT remove a part that serves as the sole connection or support between two other parts of the object. If removing it would cause another part to become physically detached, floating, or structurally unsupported, choose a different part.
    Clarification: handles, legs, arms, fins, antennas, spouts, etc. are valid removal targets — they do NOT count as "sole connections" even if small accessories (loops, hooks, clips) are attached to them.
- SURFACE CLOSURE: If the object has a closed/sealed surface (containers, bottles, housings, boxes, jars, tanks), do NOT remove parts that seal an opening (lids, caps, covers, doors, end caps, plugs). Their removal would leave a hole in the surface, making the resulting 3D model non-watertight. Instead, prefer removing externally protruding parts (handles, spouts, knobs, legs, fins, antennas). This rule does not apply to inherently open or non-enclosed objects (clothing, fabric, towels, plants, tools).
- Use the form: "Remove the <part> from the <object>."

Good remove examples:
- "Remove the tail fin from the aircraft."
- "Remove the handle from the mug."
- "Remove the handle from the jar." (surface stays closed)
- "Remove the wheels from the wagon." (NOT: "Remove the front-left wheel.")

Bad remove examples:
- "Remove the lid from the container." (leaves a hole — non-watertight)
- "Remove the cap from the bottle." (leaves a hole — non-watertight)
- "Remove the curtain rings from the curtain rod." (rings connect curtain to rod — removing them detaches the curtain)
- "Remove the crossbar connecting the chair legs." (legs become structurally unsupported)
- "Remove the small button from the device." (too small / subtle)
- "Remove the logo from the object." (logo / label)
- "Remove the seam line from the ball." (surface detail)

════════════════════════════════
REPLACE RULES (type = "replace")
════════════════════════════════
- Focus on SHAPE changes only. Do not suggest color, material, texture, or finish changes.
- The new part must be a solid physical object. Do NOT replace with smoke, fire, light, shadow, energy, airflow, or any intangible effect.
- Do NOT add a brand-new part that did not exist before — only replace an existing part.
- The replacement must produce a clearly noticeable visual change. Before and after must look obviously different in silhouette or overall shape. Do NOT propose changes where the difference is subtle (e.g., slightly thicker rod, marginally shorter leg, nearly identical geometry).
- TOPOLOGY RULE: Topological changes (straight → curved, flat → domed, open → closed) are allowed ONLY when the target part appears as a SINGLE instance in the object. Do NOT apply topology changes to parts that appear multiple times (e.g., multiple legs, multiple rods, multiple panels) — the image editing model cannot maintain geometric consistency across 6 views simultaneously.
  - Allowed: replacing ONE flat roof with a domed roof; ONE cylindrical tower with a square tower.
  - Forbidden: replacing MULTIPLE rods with arches; MULTIPLE legs with curved legs; MULTIPLE panels with curved surfaces.
- Use the form: "Replace the <part> with <new part>."

Good replace examples:
- "Replace the round wheels with square wheels." (multiple instances, same topology: circle → square ✓)
- "Replace the cylindrical legs with square block legs." (multiple instances, same topology ✓)
- "Replace the pointed roof with a flat roof." (single part, topology change allowed ✓)
- "Replace the circular base with a square base." (clearly different silhouette ✓)
- "Replace the wheels with skids." (obviously different shape ✓)

Bad replace examples:
- "Replace the four vertical rods with four arches." (multiple instances + topology change ✗)
- "Replace the legs with curved legs." (multiple instances + topology change ✗)
- "Replace the thin rod with a slightly thicker rod." (not visually obvious enough ✗)
- "Replace the wooden handle with a metal handle." (material swap ✗)
- "Replace the handle with smoke." (intangible ✗)
- "Replace the entire object with a different object." (main body ✗)

═══════════════════
DIVERSITY RULES
═══════════════════
- Each instruction must target a meaningfully different part or structural change.
- Do not repeat the same edit idea with small wording changes.
- Do not repeat any instruction listed in "Previous instructions to avoid" below.

Previous instructions to avoid:
{avoid_list_block}

═══════════════════
RETURN FORMAT
═══════════════════
Return one JSON object only, with exactly these top-level keys:
- The output must begin with `{{` and end with `}}`.
- Do NOT wrap the JSON in Markdown code fences such as ```json or ```.
- Do NOT add any explanation, title, preface, or trailing note before or after the JSON.

{{
  "type_judgment": {{
    "allowed_types_used": [...],
    "preferred_types": [...],
    "reason_short": "..."
  }},
  "instructions": [
    {{"type": "remove" | "replace", "instruction": "..."}},
    ...
  ]
}}

- "instructions" must contain exactly {count} items.
- "instruction" must be one sentence only.
- Do not output anything outside the JSON object."""


# =============================================================================
# Caption Generator - 用于生成图像描述
# =============================================================================

CAPTION_GENERATOR_PROMPT = """Describe the main subject of this image in detail. \
Focus on its physical appearance, geometry, style, and key features. \
Return ONLY the description text, no explanations."""


# =============================================================================
# Fallback Templates - 用于优化失败时的备选模板（已更新为正常纹理）
# =============================================================================

FALLBACK_MATERIALS = [
    "polished metal", "brushed stainless steel", "natural wood grain",
    "smooth matte plastic", "textured rubber", "anodized aluminum",
    "carbon fiber composite", "tempered glass", "leather-wrapped"
]

FALLBACK_LIGHTING = [
    "professional studio lighting", "soft diffused lighting with rim highlights",
    "product photography lighting", "three-point lighting setup"
]

FALLBACK_TEMPLATE = """A high-end product photography of a {subject}, {material} finish, \
intricate geometric details, complex multi-part structure. {lighting}, clean white background, \
three-quarter view, photorealistic quality, sharp focus on structural details."""

# Stage 1 fallback (object-only, no camera/lighting/composition)
FALLBACK_OBJECT_DESCRIPTION = """A {subject} with a {material} finish, intricate geometric details, \
complex multi-part structure, and clearly defined components and joints."""


# =============================================================================
# Helper Functions
# =============================================================================

def get_instruction_prompt(
    edit_type: EditType = EditType.REMOVE,
    avoid_list: Optional[List[str]] = None
) -> str:
    """
    Build instruction generation prompt for a specific edit type.
    
    Args:
        edit_type: Type of edit (REMOVE or REPLACE)
        avoid_list: List of previous instructions to avoid repetition
        
    Returns:
        Complete prompt string
    """
    if edit_type == EditType.REMOVE:
        prompt = INSTRUCTION_REMOVE_PROMPT
    elif edit_type == EditType.REPLACE:
        prompt = INSTRUCTION_REPLACE_PROMPT
    else:
        # Default to REMOVE for backward compatibility
        prompt = INSTRUCTION_REMOVE_PROMPT
    
    if avoid_list and len(avoid_list) > 0:
        # Only include last 5 to avoid prompt being too long
        avoid_text = "\n".join([f"- {inst}" for inst in avoid_list[-5:]])
        prompt += INSTRUCTION_AVOID_TEMPLATE.format(avoid_list=avoid_text)
    
    return prompt


def get_batch_instruction_prompts(avoid_list: Optional[List[str]] = None) -> List[Tuple[EditType, str]]:
    """
    Get prompts for batch instruction generation (one REMOVE + one REPLACE).
    
    This ensures a 1:1 ratio of remove:replace instructions.
    
    Args:
        avoid_list: List of previous instructions to avoid repetition
        
    Returns:
        List of (EditType, prompt_string) tuples
    """
    return [
        (EditType.REMOVE, get_instruction_prompt(EditType.REMOVE, avoid_list)),
        (EditType.REPLACE, get_instruction_prompt(EditType.REPLACE, avoid_list)),
    ]


def get_adaptive_instruction_prompt(
    count: int,
    allowed_types: List[str],
    avoid_list: Optional[List[str]] = None,
) -> str:
    allowed_types_block = "\n".join(f"- {instruction_type}" for instruction_type in allowed_types)
    if avoid_list:
        avoid_list_block = "\n".join(f"- {instruction}" for instruction in avoid_list[-10:])
    else:
        avoid_list_block = "- None"
    return INSTRUCTION_ADAPTIVE_K_PROMPT.format(
        count=count,
        allowed_types_block=allowed_types_block,
        avoid_list_block=avoid_list_block,
    )


def get_optimize_prompt(subject: str) -> Tuple[str, str]:
    """
    Get system and user prompts for prompt optimization.
    
    Args:
        subject: The object/subject to optimize
        
    Returns:
        Tuple of (system_prompt, user_prompt)
    """
    return (
        PROMPT_OPTIMIZER_SYSTEM,
        PROMPT_OPTIMIZER_USER.format(subject=subject)
    )


def get_object_description_prompt(subject: str) -> Tuple[str, str]:
    """
    Get system and user prompts for stage-1 object description.
    No style hint - style is applied directly to final T2I prompt.
    """
    return (
        OBJECT_DESCRIPTION_SYSTEM,
        OBJECT_DESCRIPTION_USER.format(subject=subject)
    )


def get_image_requirements_prompt() -> str:
    """Get fixed image requirements for stage-2 composition."""
    return IMAGE_REQUIREMENTS_PROMPT


def compose_t2i_prompt(
    object_description: str,
    style_prefix: Optional[str] = None,
    image_requirements: Optional[str] = None
) -> str:
    """
    Compose the final T2I prompt:
    [Style prefix] + [Object description] + [Image requirements]
    
    Style prefix is placed FIRST so T2I model sees the style directive clearly.
    """
    parts = []
    
    # Style prefix first (direct style control for T2I)
    if style_prefix:
        parts.append(style_prefix.strip())
    
    # Object description
    if object_description:
        desc = object_description.strip()
        if desc and not desc.endswith("."):
            desc += "."
        parts.append(desc)
    
    # Image requirements last
    if image_requirements is None:
        image_requirements = IMAGE_REQUIREMENTS_PROMPT
    if image_requirements:
        parts.append(image_requirements.strip())
    
    return " ".join(parts)


def get_fallback_prompt(subject: str, material: str = None, lighting: str = None) -> str:
    """
    Generate a fallback prompt when optimization fails.
    
    Args:
        subject: The object/subject
        material: Optional specific material (random if not provided)
        lighting: Optional specific lighting (random if not provided)
        
    Returns:
        Fallback prompt string
    """
    import random
    
    if material is None:
        material = random.choice(FALLBACK_MATERIALS)
    if lighting is None:
        lighting = random.choice(FALLBACK_LIGHTING)
    
    return FALLBACK_TEMPLATE.format(
        subject=subject,
        material=material,
        lighting=lighting
    )


def get_fallback_object_description(subject: str, material: str = None) -> str:
    """
    Generate a fallback object-only description for stage-1.
    """
    import random

    if material is None:
        material = random.choice(FALLBACK_MATERIALS)

    return FALLBACK_OBJECT_DESCRIPTION.format(
        subject=subject,
        material=material,
    )


# =============================================================================
# Stage2 VLM Reconstruction Consistency Check
# =============================================================================

STAGE2_VLM_RECON_PROMPT = """\
You will receive two images and one editing instruction:
- Image 1: A 3×2 grid of EDITED REFERENCE images (6 views: front/back/left/right/top/bottom). \
This represents the 2D target appearance after editing.
- Image 2: A 3×2 grid of TARGET 3D MODEL renders (same 6 views). \
This is the actual 3D reconstruction result.
- Editing instruction: "{instruction}"

Your task: Judge whether the target 3D model faithfully reconstructs the edited 2D appearance.

Focus on these criteria (in order of importance):
1. The edit described in the instruction is visually present in the 3D renders \
(e.g., if a part was removed, it should be absent; if something was added, it should be visible).
2. The overall shape and structure match between the reference images and the 3D renders.
3. No severe reconstruction artifacts (melting geometry, collapsed shape, severe blurring).

Do NOT penalize for:
- Minor texture or color differences (lighting variation between 2D and 3D is expected).
- Slight geometry simplification.

Respond ONLY with valid JSON (no markdown, no explanation outside the JSON):
{{"pass": true/false, "confidence": 0.0, "reason": "one sentence, max 30 words"}}
"""


STAGE2_VLM_RECON_PROMPT_WITH_SOURCE = """\
You will receive three images and one editing instruction:
- Image 1: A 3×2 grid of the ORIGINAL SOURCE 3D model renders (6 views: front/back/right/left/top/bottom). \
This is the object BEFORE editing.
- Image 2: A 3×2 grid of EDITED REFERENCE images (same 6 views). \
This is the 2D target appearance AFTER the edit was applied.
- Image 3: A 3×2 grid of TARGET 3D MODEL renders (same 6 views). \
This is the actual 3D reconstruction from the edited images.
- Editing instruction: "{instruction}"

Your task: Judge whether the target 3D model (Image 3) faithfully reconstructs the edited 2D appearance (Image 2).

Step 1 — Locate the edit: Compare Image 1 (source) with Image 2 (edited reference) to identify \
exactly which part of the object was changed by the instruction. Focus ONLY on that part for the edit check.

Step 2 — Verify the edit in the reconstruction: Check whether Image 3 (target 3D) correctly \
shows the same edit that appears in Image 2. The specific part identified in Step 1 should look \
consistent between Image 2 and Image 3.

Step 3 — Check non-edited regions: Parts of the object that were NOT changed between Image 1 and \
Image 2 should also remain unchanged in Image 3. Do not penalize the reconstruction for changes \
in regions that were already different between Image 1 and Image 2.

Do NOT penalize for:
- Minor texture or color differences (lighting variation between 2D and 3D is expected).
- Slight geometry simplification.

Fail ONLY if:
- The edited part in Image 3 clearly does not match Image 2 (edit not reconstructed).
- Severe reconstruction artifacts (melting geometry, collapsed shape, severe blurring).

Respond ONLY with valid JSON (no markdown, no explanation outside the JSON):
{{"pass": true/false, "confidence": 0.0, "reason": "one sentence, max 30 words"}}
"""


# =============================================================================
# Organized PROMPTS dict for easy access
# =============================================================================

PROMPTS = {
    "optimize_prompt": {
        "system": PROMPT_OPTIMIZER_SYSTEM,
        "user_template": PROMPT_OPTIMIZER_USER,
    },
    "object_description": {
        "system": OBJECT_DESCRIPTION_SYSTEM,
        "user_template": OBJECT_DESCRIPTION_USER,
    },
    "image_requirements": {
        "prompt": IMAGE_REQUIREMENTS_PROMPT,
    },
    "instruction_generator": {
        "remove": INSTRUCTION_REMOVE_PROMPT,
        "replace": INSTRUCTION_REPLACE_PROMPT,
        "avoid_template": INSTRUCTION_AVOID_TEMPLATE,
        "adaptive_k": INSTRUCTION_ADAPTIVE_K_PROMPT,
    },
    "caption_generator": {
        "prompt": CAPTION_GENERATOR_PROMPT,
    },
    "fallback": {
        "template": FALLBACK_TEMPLATE,
        "materials": FALLBACK_MATERIALS,
        "lighting": FALLBACK_LIGHTING,
    },
    "fallback_object_description": {
        "template": FALLBACK_OBJECT_DESCRIPTION,
        "materials": FALLBACK_MATERIALS,
    },
}


# =============================================================================
# Multiview Editing Guardrail Prompts
# =============================================================================

MULTIVIEW_GUARDRAIL_V2 = """\
Preserve all non-target content, geometry, materials, and appearance exactly.
Apply only the requested edit, and restrict changes to the smallest necessary region.
Do not modify the shape, structure, geometry, or appearance of any part of the object except the specific target part mentioned in the instruction.
Follow the instruction literally. Do not make any additional changes beyond exactly what is specified.
If replacing a part, the replacement must visually match the style, scale, material, and rendering quality of the rest of the original object.
Keep the overall composition, camera viewpoint, object placement, layout, and labels unchanged.
Do not change the overall lighting, shading, shadows, or rendering tone of the image. Only the geometry of the target part should change.
Do not generate any shadows on the background (no cast shadows, drop shadows, or ground shadows). The output must remain a clean, shadow-free product render on a pure white background.
The background must remain pure white (#FFFFFF). Do not introduce any background color shift, gradient, or tint.
If the target is not visible in a view, keep that view pixel-identical to the input.
Blend edited regions naturally with neighboring areas.
Do not introduce unintended new parts, details, textures, or artifacts.
The edited result must be physically plausible. Do not produce results where parts of the object are disconnected, floating, or structurally unsupported as a consequence of the edit."""

_GUARDRAIL_REGISTRY: dict[str, str] = {
    "mv_guardrail_v2": MULTIVIEW_GUARDRAIL_V2,
}


def get_guardrail_text(version: str) -> str:
    """Look up guardrail prompt text by version key.

    Raises KeyError if the version is not registered.
    """
    if version not in _GUARDRAIL_REGISTRY:
        raise KeyError(
            f"Unknown guardrail version: {version!r}. "
            f"Available: {list(_GUARDRAIL_REGISTRY)}"
        )
    return _GUARDRAIL_REGISTRY[version]
