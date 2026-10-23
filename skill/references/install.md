# EvoH3 Plugin — Portable Installation

## Requirements

| item | requirement |
|---|---|
| ComfyUI | ≥ 0.3.x with `comfy_api.latest` v0.4 IO schema (`IO.ComfyNode`). Check: `python -c "from comfy_api.latest import IO"` in the ComfyUI env |
| Python | ≥ 3.10 (plugin uses `X \| None` unions) |
| Deps | `aiohttp` only — bundled with ComfyUI |
| Server | an H3-compatible OpenAI-style endpoint. Two profiles: |

### Server endpoint matrix (pick what your base_url must answer)

| node | endpoints needed |
|---|---|
| First/Last Frame | `POST /v1/videos` (multipart `input_reference`), `GET /v1/videos/{id}`, `GET /v1/videos/{id}/content` |
| Reference Images (max 9) | **+ `POST /v1/files`** (returns `{id: "file_…"}`), flat JSON submit |
| Reference + Voice | same as Reference Images (+ audio/video fields in payload) |

A relay that only implements the first row (no `/v1/files`) supports the FL2VA and
First-Last Frame nodes only — that is why those exist. The two reference nodes need a
**gateway that implements `POST /v1/files`** (the h3-gateway family does).

## Two possible layouts

This skill ships in two places; the paths differ, the steps do not.

| you got it from | plugin root is | installer |
|---|---|---|
| the GitHub repo (`git clone`) | the repo root itself | none needed — clone into `custom_nodes` |
| the portable zip | `plugin/` inside the zip | `./install.sh` |

**Whatever you copy, copy the whole directory including `web/`.** It holds
`web/js/duration_slider.js`, which retargets the `duration` slider when `long_mode`
flips. Copying only the `.py` files still runs, but the slider will not change range.

## Install methods

### A. install.sh (portable zip layout)

```bash
./install.sh /path/to/ComfyUI          # copy into custom_nodes/
./install.sh /path/to/ComfyUI --link   # symlink for live editing
```

The script also: checks aiohttp in the ComfyUI venv, `py_compile`s the package,


### B. Manual (either layout)

```bash
# from the portable zip
cp -R plugin/ <ComfyUI>/custom_nodes/ComfyUI-EvoH3Node
# from a checkout of the repo
cp -R . <ComfyUI>/custom_nodes/ComfyUI-EvoH3Node
```

Directory **must** be named `ComfyUI-EvoH3Node` (import path `from .evo_h3 import …`).

### C. Git (recommended once published)

```bash
cd <ComfyUI>/custom_nodes && git clone <repo-url> ComfyUI-EvoH3Node
```

ComfyUI-Manager → **Install via Git URL** works with the same URL, and makes updates a
`git pull`.

## Verify

1. Restart ComfyUI; log must show the custom node list without an `IMPORT FAILED` for ComfyUI-EvoH3Node.
2. Node search (double-click canvas) finds `Evo H3 API Key`.
3. Minimal smoke test: Key node (base_url + key) → First/Last Frame (any LoadImage, duration 4, short prompt) → Save Video → Run. 4s clip proves auth + submit + poll + download end to end.

## API key setup

Preferred: `export EVOH3_API_KEY="sk-…"` before launching ComfyUI from a terminal,
leave the node's `api_key` empty.
⚠️ ComfyUI Desktop started from Dock/Finder does **not** inherit shell env —
that's the #1 cause of "EvoH3 API key is required" after a "working" setup.
Alternative: launch Desktop from a terminal, or fill the node field (plaintext in JSON —
scrub before sharing: both `widgets_values` and `widgets_values_named`).

## Uninstall

`rm -rf <ComfyUI>/custom_nodes/ComfyUI-EvoH3Node` (or remove the symlink). No state
outside the directory; the key lives in env or the workflow JSON only.
