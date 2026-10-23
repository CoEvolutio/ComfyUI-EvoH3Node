---
name: evo-h3-plugin
description: Install and use the EvoH3 ComfyUI plugin (ComfyUI-EvoH3Node) — MiniMax H3 video generation nodes that talk to an OpenAI-compatible relay/gateway. Use when setting up the plugin on a new machine, wiring the four nodes (API Key / First-Last Frame / Reference Images max 9 / Reference + Voice), making clips longer than 10 seconds with the built-in long-video mode, keeping a character and a voice consistent across a long render, fixing "EvoH3 API key is required" or "no output node" errors, or loading the bundled de-keyed example workflows.
compatibility: Any agent that can read local files and run shell commands. Requires ComfyUI >= 0.3.x with comfy_api v0.4 IO schema support, and a reachable H3-compatible API endpoint. The plugin itself only needs aiohttp (bundled with ComfyUI).
---

# EvoH3 Plugin — Install & Use

```
plugin/      the ComfyUI custom-node package (ComfyUI-EvoH3Node)
skills/      this skill
install.sh   portable installer
```

## 0. Before anything else: you need an endpoint and a key

The plugin talks to **a MiniMax-H3-compatible gateway, not to a public service**.
`base_url` and `api_key` are **issued by whoever runs the gateway** — they are not in
this repo and there is no default that will work. Ask the operator for:

| what | example | where it goes |
|---|---|---|
| `base_url` | `https://your-gateway.example.com` (no trailing `/v1`) | `Evo H3 API Key` → `base_url` |
| `api_key` | `sk-…` | `Evo H3 API Key` → `api_key`, or env `EVOH3_API_KEY` |

No workflow in this repo contains a key or an endpoint. If you share one of yours,
scan it first: `grep -rE "sk-[A-Za-z0-9_-]{10,}" *.json` — a real key inside
`widgets_values` **and** `widgets_values_named` is a leaked key.

## 1. Install

Clone the repo straight into `custom_nodes` (or use ComfyUI-Manager → Install via Git URL):

```bash
cd <ComfyUI>/custom_nodes
git clone <repo-url> ComfyUI-EvoH3Node
```

From the portable zip instead, run `./install.sh /path/to/ComfyUI` (`--link` to symlink).
**Copy the whole directory, including `web/`** — it holds the JS that retargets the
`duration` slider.

Then restart ComfyUI. Nodes appear under category **EvoH3**.
`references/install.md` has the full procedure and the failure table.

Most common failure: ComfyUI too old (`comfy_api.latest` / `IO.ComfyNode` missing).
Update ComfyUI, not the plugin.

## 2. Every graph needs an output node

All four nodes output `VIDEO`. ComfyUI rejects a graph with no output node
("工作流未包含任何输出节点" / "prompt has no outputs"). **Terminate with a core
`Save Video` node** — connect the node's `VIDEO` output to `Save Video` → `video`.
This is the single most common "it doesn't run" cause.

## 3. Which node

| Need | Node |
|---|---|
| Animate a given first frame (± last frame) | `Evo H3 First/Last Frame to Video` |
| Character/style from photos only | `Evo H3 Reference Images (max 9)` |
| Photos **plus voice/timbre reference** | `Evo H3 Reference + Voice (3 img + audio)` |

`reference ≠ compositing`: H3 conditions on references, it cannot paste or exactly
clone them. Identity holds best with **head-and-shoulders crops** and a prompt whose
wardrobe **matches the reference**; describing different clothing makes the model
replace both outfit and face.

## 4. Clip length: the one rule that matters

A **single pass diverges into coloured noise past ~10 s** on int8 workers — silently
(no error, progress 100 %, garbage on download). So:

| Target | `long_mode` | `duration` | `chunk_seconds` |
|---|---|---|---|
| ≤ 10 s | `off` | **seconds** (4–10) | ignored |
| > 10 s | `autocontext · 分段拼接长视频` | **NUMBER OF SEGMENTS** | seconds per segment (4–10, 8 is best) |

`duration` changes unit with the mode: **seconds in short mode, segment count in long
mode**, and total length = `duration × chunk_seconds`. With `chunk_seconds=8`,
`duration=75` is a 10-minute film. The slider's range and label follow `long_mode`
automatically; the ceiling is **682 s** (the plugin's `total_frames` grid limit of
16376 frames ÷ 24 fps = 85 segments at 8 s).

Long renders are chunked and stitched **inside the plugin** — no external splicing.
Peak VRAM depends on `chunk_seconds` only, so a 10-minute film costs the same VRAM as
an 8-second one. Read `references/long-video.md` before attempting one.

## 5. Keeping the character and the voice

Two switches decide this, both on the `Reference + Voice` node:

| switch | value | why |
|---|---|---|
| `ref_image_0..2` | the **same** photos on every run | identity is re-anchored per chunk |
| `ref_sync_mode` | **`global`** | `segmented` slices the reference audio per chunk — a 7 s timbre sample becomes sub-second fragments and the voice changes after chunk 1 |
| `clip_mode` | `global` (simplest) or `Clip_Tag` (camera control) | `global` re-reads one prompt per chunk, so the **shot framing jumps at each seam**; use `Clip_Tag` with `段N 8秒` tags to control the camera per segment |
| `accel` | `auto` | lets the gateway apply the measured 2.16× sparse budget on long renders only |

Prompt format matters too: reference-driven renders should use the six-section
Ref2VA format (`subject_definitions` / `summary` / `retention_analysis` /
`detailed_description` / `overall_soundscape` / `non_diegetic_music`), not the short
`integrated_multimodal_description` form. `retention_analysis` is where you state what
must stay identical.

## 6. Prompt writing: use the official skill

The MiniMax H3 project publishes an official prompt-writing skill that matches these
nodes exactly. Install it once, then every prompt goes through it:

```bash
npx skills add https://github.com/MiniMax-AI/MiniMax-H3 --skill h3-prompt-writing
```

Reference-driven renders (Ref2VA) need its **six-section** format
(`subject_definitions` / `summary` / `retention_analysis` / `detailed_description` /
`overall_soundscape` / `non_diegetic_music`) — not the short
`integrated_multimodal_description` form used by the base modes. Eight style-specific
skills (product ads, brand promos, 3D animation, paper craft, collage, MV, game intro,
hand-drawn) install with `--skill '*'`. Index and commands in
[`official/README.md`](official/README.md).

## 7. References

- `references/install.md` — install, upgrade, uninstall, failure table
- `references/nodes.md` — every widget of every node, with ranges and defaults
- `references/long-video.md` — long renders: parameters, `Clip_Tag`, frame math, cost
- `references/multi-segment.md` — when you *do* want separate files (beyond 682 s, or per-shot control), and how to assemble them

## 8. Progress and status

The progress bar sticks at 30 % while generating — the relay reports only 20/30/100.
Judge by the status text, not the bar. A long render shows per-chunk progress in the
ComfyUI console (`[H3-Auto] 生成段 3/75 …`).
