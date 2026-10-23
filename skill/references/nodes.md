# EvoH3 Plugin — Node Reference

Source of truth: `plugin/evo_h3.py` (v1.1.0, 4 registered nodes).
All outputs are ComfyUI `VIDEO` — **always terminate with a core `Save Video` node**,
otherwise ComfyUI refuses to run the graph ("no output node").

## Shared: `Evo H3 API Key` (`EvoH3KeyNode`)

| widget | meaning |
|---|---|
| `base_url` | relay root, no trailing `/v1`. No working default — the operator issues this address; the shipped placeholder is rejected with a clear message |
| `api_key` | bearer token; **empty ⇒ reads env `EVOH3_API_KEY`** |
| `model` | resolution is the model choice: `auto · prefer 2K` (default) / `auto · prefer 768P` / `minimax-h3-2k` / `minimax-h3-768p` / `MiniMax-H3` / `custom…` |
| `model_custom` | exact id, used only when `model = custom…` |

`auto` queries `GET /v1/models` (5-min cache), falls back to `minimax-h3-768p` with a log warning.

## `Evo H3 First/Last Frame to Video` (`EvoH3FirstLastFrameNode`)

- Inputs: `key`, `prompt`, `first_frame_url`, `last_frame_url`(adv), `size`, `duration` (4–10, default 4), `watermark`, **`first_frame` IMAGE (optional)**
- Connected `first_frame` tensor ⇒ auto **multipart `input_reference`** upload to `POST /v1/videos` (works on relays without file API). URL fields are the fallback.
- FL2VA mode: both first+last ⇒ path between frames; first only ⇒ I2VA forward development.
- `size` is ignored by the server when a first frame is supplied (ratio follows the image).
- **Server needs**: `POST /v1/videos` (multipart) + `GET /v1/videos/{id}` + `GET /v1/videos/{id}/content`.

## `Evo H3 Reference Images (max 9)` (`EvoH3ReferenceImagesNode`)

- Inputs: `key`, `prompt`, `size`, `duration` (default 5), `seed` (adv, −1 random), `quality` (adv), `steps` (adv, default 4), `upscale` (adv), `watermark` (adv), `ref_image_0..8` IMAGE
- Each image: PNG-encode → `POST /v1/files` → `file_…` id → `reference_images[]` in the flat JSON submit.
- **Server needs `POST /v1/files`** — evoh3 does NOT implement it (404). Works against a self-hosted h3-gateway-style server. On evoh3, use the FL2VA node instead.
- prompt references: `<Picture 1>` … `<Picture 9>` (1-based, in slot order of connected images).

## `Evo H3 Reference + Voice (3 img + audio)` (`EvoH3ReferenceVoiceNode`)

- Same widgets as above **plus** `ref_image_0..2` (hard cap 3), `ref_video` VIDEO, `ref_video_audio` AUDIO, `ref_audio` AUDIO.
- `ref_audio` connected ⇒ request carries `reference_audios` ⇒ server-side **motion branch** semantics.
- Validation: ref_audio requires ≥1 image or video (voice must bind to a face).
- prompt references: `<Picture N>`, `<Video 1>`, `<Audio 1>`.

### quality / steps / upscale caveats (measured 2026-10-21)

| widget | reality |
|---|---|
| `quality` | `delivery · DMAD 4-step` = omit lora (server default); `draft` sends a per-mode turbo LoRA (ref2v vs fl2v chosen by connected media — mismatched LoRA = 0 params hit = garbage); `raw` = no LoRA 8-step |
| `upscale` | honored on the plain path; **silently dropped on motion branches of some gateways** |
| `steps` | may be clamped by the server (motion branch forces 8 on some builds) |

Check what actually ran via the job's echoed `params`, not the widget values.

## size options

`auto · relay default` (omit size) · `16:9` · `9:16` · `1:1` · `4:3` · `3:4` · `21:9` ·
`1344x768 (delivery 16:9)` · `768x1344 (delivery 9:16)` · `1280x720 (768P)` · `720x1280 (768P)` · `1024x1024 (768P)`

## duration

4–10s (`EVOH3_MAX_SECONDS` env can lower the cap client-side).
**Production: 8s.** 10s = 243 frames; int8/Triton workers noise out from ~9.4s.

## Progress semantics

Node status: `Submitting → Queued → Generating → Downloading` + elapsed time.
Relay progress is 3 coarse steps (queued 20 / generating 30 / done 100) —
**stuck at 30 is normal**, trust the status word.

## Long-video widgets (all three generation nodes)

| widget | type | default | used when | meaning |
|---|---|---|---|---|
| `long_mode` | combo | `off` | always | `off` = single pass (≤10 s); `autocontext · 分段拼接长视频` = chunked long render |
| `duration` | int | 5 | always | **seconds** when `long_mode=off` (4–10); **segment count** when long (1–⌊682÷chunk⌋) |
| `chunk_seconds` | int | 8 | long only | seconds per chunk, 4–10 |
| `context_frames` | int | 22 | long only | tail frames reused for seam continuity (17n+5 grid; try 39 if a seam shows) |
| `clip_mode` | combo | `global` | long only | `global` / `Clip_Tag` / `timeline` |
| `ref_sync_mode` | combo | `global` | long only, **Voice node only** | reference audio per chunk: `global` = whole sample (keeps the voice); `segmented` = sliced (breaks it) |
| `accel` | combo | `auto` | long only | `auto` / `0.3` / `0.5` / `off` — attention sparsity; `auto` applies 0.3 on long renders |

### `duration` unit change (important)

```
long_mode = off          duration = seconds,       range 4–10
long_mode = autocontext  duration = segment count, range 1–floor(682 / chunk_seconds)
                         total seconds = duration * chunk_seconds
```

With `chunk_seconds = 8`: `duration=75` → 600 s, `duration=85` → 680 s (ceiling),
`duration=86` → rejected with a message naming the actual maximum.

The widget range and label are retargeted by `web/js/duration_slider.js`; the slider text
reads `duration（秒）` in short mode and `duration（段数）` in long mode. If the frontend
script did not load (hard-refresh the browser), submission is still validated.

### Node input counts (v1.1.0)

| node | inputs |
|---|---|
| `Evo H3 Reference Images (max 9)` | 23 |
| `Evo H3 Reference + Voice (3 img + audio)` | 21 |
| `Evo H3 First/Last Frame to Video` | 13 |
