# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is an automated 2D-to-3D dataset generation pipeline. It produces paired datasets of (source 3D model) → (editing instruction) → (target 3D model) via a 5-stage process:
1. **Prompt & T2I** — Generate prompts and reference images via LLMs/image models
2. **Source Gen3D** — Convert images to 3D models (Hunyuan, Tripo, Rodin)
3. **Multi-View Render** — Render 6 standard views via Blender or WebGL
4. **Edit** — Generate edit instructions via MLLM, apply multi-view consistent image edits
5. **Target Gen3D** — Generate new 3D from edited views, run quality checks (LPIPS)

## Environment & Running

```bash
# Activate environment
# qh_k8s (current primary server):
source /data-koolab-nas/xiaoliang/local_envs/2d3d/bin/activate
# qh_4_4090 (old server):
# source /home/xiaoliang/local_envs/2d3d/bin/activate

# Start Web UI — access at http://<server-ip>:10002
# Log to data-koolab-nas to avoid data-oss OSError flush issues
nohup /data-koolab-nas/xiaoliang/local_envs/2d3d/bin/python \
  /data-oss/meiguang/xiaoliang/code3/2d3d_v2/app.py \
  > /data-koolab-nas/xiaoliang/code3/2d3d_v2/logs/app.log 2>&1 &
```

## CLI Commands

All batch operations go through `scripts/batch_process.py`:

```bash
# Generate 3D models from images
python scripts/batch_process.py gen3d --provider hunyuan
python scripts/batch_process.py gen3d --provider hunyuan --ids id1 id2 --force

# Render multi-view images for models
python scripts/batch_process.py render
python scripts/batch_process.py render --ids id1 id2

# Edit views using per-model instructions
python scripts/batch_process.py edit                            # instruction index 0 (Remove)
python scripts/batch_process.py edit --instr-index 1           # instruction index 1 (Replace)
python scripts/batch_process.py edit --all-instructions --max-per-type 1
python scripts/batch_process.py edit --mode multiview          # multi-view stitched mode

# Generate Target 3D from edited views
python scripts/batch_process.py gen3d-from-edits --provider hunyuan

# Quality checks
python scripts/batch_process.py check-edit-quality --ids <model_id>
python scripts/batch_process.py check-target-consistency --provider hunyuan --ids <model_id> --edit-id <edit_id> --skip-render

# Maintenance
python scripts/batch_process.py materialize-edit-artifacts --ids <model_id>

# All batch commands support --dry-run (preview) and --force (re-run even if done)
```

Single-asset scripts:
```bash
python scripts/gen3d.py <image_id> --provider hunyuan
python scripts/render_views.py <model_id>
python scripts/run_full_experiment.py path/to/experiment.yaml
```

## Testing

Tests live in `tests/` (never in `scripts/`):
```bash
python -m pytest tests/test_*.py
```

## Architecture

### Code Organization (strictly enforced)
- `app.py` — Flask Web UI entry point (7000+ lines)
- `config/config.yaml` — **Single source of truth for all parameters**; `config/config.py` parses it
- `core/` — Business logic: `image/` (T2I, editing, caption), `gen3d/` (Tripo, Hunyuan, Rodin clients), `render/` (Blender scripts, semantic alignment, quality checking)
- `utils/` — Shared infrastructure: `llm_client.py`, `image_api_client.py`, `prompts.py`, `paths.py`, `blender.py`
- `scripts/` — Production pipeline CLI scripts only (no `test_*` files allowed here)
- `tests/` — All test/debug/one-off scripts

### Key Client Layers
- `utils/llm_client.py` — All text LLM calls (GPT, Gemini, etc.)
- `utils/image_api_client.py` — All image API calls; auto-selects API style by model name:
  - `gemini-*`, `imagen-*`, `doubao-*` → Response API (async polling)
  - Other models → Chat Completions API (synchronous)
- `utils/prompts.py` — All prompt templates; import `EditType`, `get_instruction_prompt`, etc.

### Data Layout
```
data/pipeline/
├── prompts/           # .jsonl prompt files
├── images/{id}/       # image.png, meta.json, instructions.json
├── models_src/{id}/   # model_hy3.glb, meta.json
└── triplets/{id}/
    ├── views/{provider_id}/   # front/back/left/right/top/bottom.png
    └── edited/{edit_id}/      # edited views + meta.json
```

## Critical Principles

### Fail Loudly (highest priority)
Never mask errors or silently degrade. Config mismatches must throw immediately.

```python
# WRONG — silent fallback
samples = render_data.get('samples', 64)
model_config = models.get(name) or models['default']

# CORRECT — loud failure
samples = _require_key(render_data, 'samples', 'render')
model_config = models[name]  # KeyError if not exists
```

### Single Source of Truth for Configuration
All parameters are defined in `config/config.yaml` only. Code must not set `.get(key, default)` for any key already defined in config. If a key is missing from config, the program must fail immediately, not silently use a hardcoded fallback.

The `workspace` section must contain all server-specific absolute paths:
- `pipeline_dir` — data root
- `python_interpreter` — used by Web UI to launch experiment subprocesses
- `playwright_browsers_path` — WebGL renderer browser cache (must be on local disk, not OSS/SeaweedFS)
- `logs_dir` — experiment run logs (must be on local disk to avoid OSS flush errors)

### Documentation Update Rules
When changing behavior, CLI args, or config keys, synchronize:
- `CHANGELOG.md` (all changes)
- `docs/guide/cli.md` (CLI args)
- `README.md` (major feature changes)
- `AGENTS.md` (architecture/standards changes)
