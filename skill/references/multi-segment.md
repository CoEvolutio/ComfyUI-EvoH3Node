# Multi-Segment Generation & Assembly

> **Read `long-video.md` first.** A single node run can now produce up to 682 seconds
> by chunking internally (`long_mode = autocontext`), so most "long film" work no longer
> needs external segment files. Come back here only when you need:
> - a film **longer than 682 s** (the plugin's frame-grid ceiling),
> - per-shot **hard cuts** as an editorial choice,
> - segments rendered on different machines/sessions.
>
> Use long mode by default; use this document for true multi-file projects.

## Segment planning (prompt side)

- One action arc per segment: setup → speak → settle. 8s cannot hold two turns.
- One shot-size change per segment, executed by camera move, not a cut.
- Speech ≈ 18–20 Chinese characters (~4–5s) to leave lip-sync and breath room.
- End every segment on a "freeze + micro-expression" — makes hard cuts read as edits.

## Two chaining mechanisms

### 1. Independent Ref2VA segments + hard cut

Every segment is a full `Evo H3 Reference + Voice` node with the same 3 reference
views + same timbre slice, different prompts, per-segment seeds.

- Cuts are intentional edit points (see planning rules above).
- Timbre is weakly anchored (`ref_audios` ≈ guidance, not lock — measured f0 drifts
  across scenes). Character identity stays stable (measured drift 0.44→0.34→0.36, no accumulation).
- Scales to N segments trivially (copy-paste node group).

### 2. FL2VA first-frame continuation

Segment 1 = Ref2VA node. Segments 2..N = `First/Last Frame` node with
`GetVideoComponents → ImageFromBatch(batch_index=-1, length=1) → first_frame`
chained from the previous segment.

- Seam is invisible: new[0] = prev[-1], NCC 0.9983, **zero frame trimming**.
- ⚠️ The FL2VA node has **no audio input** — from segment 2 on there is no timbre reference.
- ⚠️ Do NOT chain via `AddGuide(last-22-frames)`-style guides on other backends:
  they replay the previous tail (22 frames ≈ 0.917s per seam).

## Assembly (ffmpeg, per segment)

Raw segments differ in loudness by 20+ dB — normalize **each segment first**:

```bash
ffmpeg -y -i seg01.mp4 -c:v copy -af loudnorm=I=-16:TP=-1.5 norm/seg01.mp4   # ×N
printf "file 'norm/seg%02d.mp4'\n" $(seq 1 N) > list.txt
ffmpeg -y -f concat -safe 0 -i list.txt -c:v copy -an video.mp4
ffmpeg -y -i video.mp4 -i seg01.mp4 -map 0:v:0 -map 1:a:0 ...   # or rebuild audio track
```

Audio: each AAC segment carries ~17ms encoder latency; ≤5 segments concat drift is
tolerable, beyond that rebuild the audio track from source slices by frame count
(192 frames @ 24fps = exactly 8s per segment).

## Seam self-check after any prompt change

```bash
ffmpeg -y -i seg01.mp4 -vf "select='eq(n,191)'" -vsync 0 a.png
ffmpeg -y -i seg02.mp4 -vf "select='eq(n,0)'"   -vsync 0 b.png
# FL2VA chain: a/b must be near-identical (NCC > 0.99); hard-cut route: they must NOT be
```

## Do not chain when long mode can do it

Chaining N nodes means N separate jobs, N chances to lose the identity, and an external
assembly step. Long mode keeps one job, one reference set, and seam anchoring inside the
model's latent space. The measured case: 32 s as 4 chained nodes drifts in framing at
every cut; 32 s in long mode held identity and voice across 5 chunks.

## Server-side motion-branch caveat

Requests containing `reference_audios` may take a different server code path
("motion") whose parameters can differ from the plain path (steps forced, upscale
dropped, LoRA swapped — observed on h3-gateway builds). After the first run of a
session, read the job's echoed `params` and compare against the widgets before
batching 50+ segments.
