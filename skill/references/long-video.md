# Long renders (more than 10 seconds)

A single H3 pass diverges into coloured noise past roughly 10 seconds of footage.
The failure is silent: no error, progress reaches 100 %, and only the downloaded file
is garbage. `long_mode = autocontext · 分段拼接长视频` avoids it by rendering the film
in chunks and stitching them **inside the plugin** — you do not splice anything.

## Parameters

| widget | 10 seconds or less | 10 minutes (600 s) | notes |
|---|---|---|---|
| `long_mode` | `off` | `autocontext · 分段拼接长视频` | |
| `duration` | **seconds**, 4–10 | **segments**, e.g. `75` | unit changes with the mode |
| `chunk_seconds` | ignored | `8` | seconds per chunk; 4–10, 8 measured best |
| `context_frames` | ignored | `22` | tail frames reused for continuity; raise to 39 if a seam is visible |
| `clip_mode` | ignored | `global` or `Clip_Tag` | see below |
| `ref_sync_mode` | ignored | **`global`** | `segmented` destroys a bound voice |
| `accel` | ignored | `auto` | `auto` = sparse 0.3 on long renders only |
| `ref_image_0..2` | your reference photos | the **same** photos | re-anchors identity every chunk |
| `ref_audio` | optional timbre sample | the **same** sample | re-anchors voice every chunk |

`duration` is a segment count in long mode, so:

```
75 segments × 8 s = 600 s = 10 minutes
85 segments × 8 s = 680 s = the ceiling
```

The ceiling is **682 s**, from the plugin's frame grid (`total_frames` max 16376 ÷ 24 fps).
At `chunk_seconds=8` that is 85 segments; at 4 it is 170. The slider retargets itself
when you change `long_mode` or `chunk_seconds`, and the node rejects an overshoot with a
message that names the maximum for your chunk size.

Anything longer than 682 s must be rendered as several files — see
`multi-segment.md`.

## `clip_mode` decides whether the camera holds

| mode | prompt handling | result at a seam |
|---|---|---|
| `global` | one prompt reused for every chunk | **framing jumps**: the same text is re-read and re-staged, so a wide shot can cut to a close-up |
| `Clip_Tag` | split by tags; each chunk gets its own text | camera stays under your control |
| `timeline` | split by time markers | same, driven by timestamps |

`Clip_Tag` syntax — the tag occupies its own line and ends with a duration:

```
段1 8秒
[full prompt for segment 1]

段2 8秒
[full prompt for segment 2]
```

In `Clip_Tag` and `timeline` the segment length comes from the **prompt**, and
`duration` / `chunk_seconds` only act as a fallback. Write the camera move explicitly in
every block ("the camera keeps pulling back at the same speed, framing unchanged"), or
the framing will drift even with per-segment prompts.

A 10-minute film needs 75 such blocks. That is a scripted job, not a manual one — generate
the blocks from a shot list, repeating the identity and wardrobe sentences verbatim in
each block and varying only action and camera.

## Keeping the character

- Feed **head-and-shoulders crops**, roughly 3:4. Full-body studio sheets give a weak
  identity signal; measured, a full-body reference produced a visibly different face.
- Make the prompt's wardrobe **match the reference photos**. Describing an outfit that
  conflicts with the pictures makes the model replace both the outfit and the face.
- Optionally raise `context_frames` to 39 for a stronger seam anchor at the same cost.

## Keeping the voice

`ref_sync_mode` is the single deciding switch:

| value | behaviour | use |
|---|---|---|
| `global` | every chunk receives the **whole** reference audio | default; keeps the timbre |
| `segmented` | reference audio is sliced by segment time range | only for face replacement / lip-sync against a real timeline — it makes a 7 s sample sub-second per chunk, so the voice changes after chunk 1 |

A 7-second timbre sample anchors *character* of the voice but not exact pitch: measured
F0 lands 25–30 % below the reference while staying within ±4 % across chunks. That is
"consistent", not "identical" — do not confuse the two when reviewing.

## Cost

| target | segments (8 s) | one GPU | four GPUs in parallel |
|---|---|---|---|
| 32 s | 4–5 | ~20 min | ~20 min |
| 2 min | 15 | ~60 min | ~20 min |
| 10 min | 75 | ~4–5 h | ~1.3 h |

Peak VRAM depends only on `chunk_seconds`, so a 10-minute render uses the same memory
as an 8-second one.

## Checklist before starting a long render

1. `long_mode` = autocontext, `duration` = segment count, `chunk_seconds` = 8.
2. `ref_sync_mode` = `global` if voice matters.
3. Reference photos are head-and-shoulders and match the prompt's wardrobe.
4. Prompt uses the six-section Ref2VA format, with `retention_analysis` naming what must
   stay identical.
5. `Save Video` node connected.
6. Do the maths once: `duration × chunk_seconds` ≤ 682.
