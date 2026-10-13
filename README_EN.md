# ComfyUI TT_MiniMaxH3

A self-contained ComfyUI toolkit for the MiniMax H3 audio-video model: lip-sync digital-human segmentation, one-click chained runs across segments, motion-context handoff, seam inspection and repair, and exact duration alignment.
Repository: [canyuevip/ComfyUI_TT_MiniMaxH3](https://github.com/canyuevip/ComfyUI_TT_MiniMaxH3)

中文 → [README.md](README.md)

> This plugin was previously named `ComfyUI_H3_Toolkit`. Since 2026-10-14 the node IDs, categories and HTTP routes all use the `TT` / `TT_MiniMaxH3` prefix. Workflows saved with `H3Kit*` node types need to be re-pointed to the new nodes.

## Overview

TT_MiniMaxH3 does not replace the official H3 nodes. It fills the three gaps that a long talking-head pipeline actually needs: **generating one segment at a time from a segment plan and chaining the runs automatically**, **handing the previous segment's tail frames/audio to the next segment**, and **merging plus inspecting the seams when the chain finishes**. It writes the same conditioning protocol as the official `MiniMaxH3ImageToVideo` / `MiniMaxH3ReferenceToVideo` nodes (`minimax_keyframes` / `minimax_refs` / nested `noise_mask`), so it stays compatible with stock H3 sampling and with the CLIP Loader (`type` must be `minimax`).

You supply the segment plan (start time, duration and transcript per segment): paste it into the console's **segment JSON** box, or link any node that outputs the same JSON — this edition ships no speech-recognition node. See [One-click chained runs](#one-click-chained-runs).

### Highlights

| Capability | Notes |
| --- | --- |
| Source-audio locking | The conditioning node encodes the voice track into the AV latent and sets its `noise_mask=0`, so sampling only generates video and lip-sync matches the source exactly |
| Keyframes + references | One node covers first/last frame, up to 9 reference images, up to 3 reference videos and up to 3 reference audios |
| Joint AV decode | One decode pass yields image frames plus generated audio, and splits reusable video/audio latents |
| Segment-plan driven | The chain console treats **segment JSON** (`start_time` / `duration` / `text` per segment) as the single source of truth, pasted or linked, and never splits by duration |
| One-click chained runs | After the graph is submitted, an in-process backend clones it per segment, advances the segment index, randomizes seeds and enqueues it — the browser can be closed |
| Motion-context handoff | The previous segment's tail is written as `minimax_keyframes` to guide the next segment |
| Exact duration alignment | Uniform frame resampling plus recomputed output frame rate, so container duration matches the source audio |
| Seam inspection and repair | Bridge-frame gating, five-dimension z-score seam report, exposure-locked merge with seam repair |
| Group switch | One integer turns the first N canvas groups on or off for batch runs |

## Main chain

```
model -> video_vae -> audio_vae --+
                                  |-- H3 AV conditioning -> KSampler -> H3 AV decode -> images / audio
segment JSON (external ASR node) --+                       ^
                                  |-- audio chain console -> per-segment prompt line
                                        (start_time / duration / prompt / filename prefix)
sampler latent -> save context latent -> load context latent -> motion context -> next segment conditioning
all segments done -> video merge / exposure-locked merge -> merged.mp4
```

* **Required links**: `model` -> `video_vae` -> `audio_vae` -> conditioning node; CLIP Loader `type` must be `minimax`.
* **Optional**: first/last frame, reference images/videos/audios, source audio (required in locked-audio mode), motion context (`context_latent` takes precedence over `context_frames`).
* **Outputs**: the conditioning node returns positive conditioning, the joint AV latent and the pass-through source audio; the decode node returns `images`, `audio`, and the split video/audio latents. Negative conditioning still comes from the stock `CLIPTextEncode`.

## Nodes

### Conditioning & decode

| Node | ID | Description |
| --- | --- | --- |
| 🎵H3 AV conditioning (voice/reference) | `TTH3AudioConditioning` | First/last frame, references and source-audio locking; outputs conditioning + AV latent |
| 🎞H3 AV decode | `TTH3AVDecode` | Decodes the joint AV latent once into frames + audio and splits video/audio latents |

### Long-video chain

| Node | ID | Description |
| --- | --- | --- |
| 🎬Audio chain console | `TTAudioChainConsole` | Reads the plan from its **segment JSON** box (pasted, or linked after converting the widget to an input), emits per-segment `start_time` / `duration` / `prompt` / prefix and drives the chained runs |
| 🎬H3 motion context | `TTH3MotionContext` | Writes the previous tail as keyframes to guide this segment |
| ✂H3 motion context trim | `TTH3MotionContextTrim` | Cuts the handoff warm-up frames, keeping only newly generated content |
| 💾H3 context latent save | `TTH3MotionContextSaveLatent` | Saves `{video, audio}` as safetensors (`h3_motion_context_av_v1`) |
| 📂H3 context latent load | `TTH3MotionContextLoadLatent` | Loads the previous segment latent by index; `clip_index=0` means first segment |
| 📝Per-segment prompt line | `TTChainPromptLine` | Takes prompt lines or `=== SEGMENT n ===` blocks following the chain index |

### Video

| Node | ID | Description |
| --- | --- | --- |
| 🎬Video merge (concat) | `TTVideoMerge` | Concatenates `segNNN` files with `-c copy` or re-encode; can be gated by the completion signal |
| 🎬Video merge (exposure lock, seam repair) | `TTExposureMerge` | Repairs segment-head brightness/cuts, then applies bounded gain against the first segment |
| 🧷Bridge frame gate | `TTBridgeFrameGate` | Scores the tail frame (Laplacian sharpness + exposure) and falls back to an earlier bridge window |
| 🩺Seam doctor | `TTSeamDoctor` | Five-dimension z-score report (motion, shift, color, sharpness, audio); diagnostic only |
| ⏱Duration crop | `TTDurationCrop` | Resamples frames to the target seconds and recomputes the output frame rate |
| 🎞HyperVAE 2x chunked loader | `TTFANHyperVAE2xChunked` | Chunked `pixel_shuffle` to cut peak VRAM (requires `comfyui-minimax-h3-audio-T8`; silently unregistered otherwise) |

### Audio / image / utility

| Node | ID | Category | Description |
| --- | --- | --- | --- |
| 🔊MiniMax H3 multi audio loader | `TTH3MultiAudioLoader` | Audio | Up to 3 paths with per-slot start/end trimming; missing slots become 1 s silence |
| Audio -> AudioPath (for WhisperX) | `AudioToAudioPath` | Audio | Writes `AUDIO` to a temp wav path so external speech-recognition tools can produce the segment plan |
| 🖼MiniMax H3 multi image loader | `TTH3MultiImageLoader` | Image | Up to 9 reference images with thumbnails, drag sorting and upload |
| 🎚Group switch (by count) | `TTGroupSwitch` | Utility | `count=N` enables the first N groups in the panel list, `0` disables all |

## Usage notes

### Locked source audio (talking head, recommended)

1. Set **audio mode** to `锁定源音` (lock source audio) and connect the voice track to **source audio** — required in this mode.
2. Connect the character image to first frame; connect a last frame if you want an end anchor (scaled with `center`).
3. Canvas defaults to `1344x768`; width and height must be divisible by 32. `length` defaults to 124 frames and rounds up onto the `17n+5` frame grid.
4. Decode with `TTH3AVDecode` to get final frames and audio track.

### Model-generated audio

Choose the `模型生成` mode, leave source audio empty and let the model generate audio. Feed references through reference images/videos/audios and cite them in the prompt with `<Picture N>` / `<Video N>` / `<Audio N>` (reference videos are 24 fps IMAGE batches with at least 5 frames, sampled at 12 fps for CLIP).

### Multi-segment handoff

1. Feed each segment's sampler latent into `TTH3MotionContextSaveLatent` with `clip_index` = current segment.
2. Read the previous one with `TTH3MotionContextLoadLatent` (`clip_index` = previous segment, `0` for the first).
3. `context_length` only accepts `5 / 22 / 39 / 56` (default 22 — only these are integer latent steps); `audio_context_length` defaults to 24 (`0` follows the video window).
4. Keep `soften_alpha` at 0: any positive value erases motion cues and produces washed-out frozen frames.
5. Wire `TTH3MotionContext.trim_frames` into `TTH3MotionContextTrim` to drop the handoff warm-up.

### One-click chained runs

The console walks through whatever **segment JSON** you feed it: three fields per segment, in seconds, optionally wrapped with a `total`.

```json
{"total": 21.0, "segments": [
  {"start_time": 0.0,  "duration": 11.2, "text": "first line of the script"},
  {"start_time": 11.2, "duration": 9.8,  "text": "second line of the script"}
]}
```

`segments` may also be a bare array; `start_time`/`start` and `text`/`prompt` are both accepted; entries with `duration <= 0` are ignored. Segment count, total duration and the full transcript are all derived from this JSON, so the console exposes no separate inputs for them.

1. Produce that JSON with your own speech-recognition step (WhisperX or similar); keep segments in the 4–15 s range that H3 accepts. `AudioToAudioPath` writes ComfyUI `AUDIO` to a wav file for external tools to read.
2. Paste it into the console's **segment JSON** box — the panel lists every segment right away. To feed it from the graph instead, right-click that input → "convert widget to input" and link any node that outputs the same JSON.
3. **Shared prompt** is one prompt used by every segment; with `识别文字并入提示词` on, the current segment's recognized speech is prepended to it.
4. **Context frames** (default 22) must match `context_length` in the motion context node. From segment 2 on, `start_time` shifts back by frames/fps seconds. Set 0 when you do not chain segments.
5. **Filename prefix** (default `h3_audio_chain`) sets the output folder `output/h3_audio_chain/segNNN_*.mp4`.
6. Click **▶ start** in the panel to submit the whole graph. The backend clones it per segment, advances the index and randomizes seeds. `segments per round` > 0 stops after that many segments; `0` runs to the end. **Stop** bumps the generation counter and drops queued items.
7. After the last segment, `TTVideoMerge` gated by the completion signal writes `merged.mp4`.

### Seam inspection and repair

* Put `TTBridgeFrameGate` after the previous segment's decode: `window` ≈ `context_length`; when the tail frame's sharpness z falls below `-tolerance` or exposure leaves 0.03–0.97, it falls back 17/34 frames to a cleaner bridge.
* `TTSeamDoctor` scores adjacent segments on five dimensions with an alert threshold of 3.0 (≈99.7% confidence band) and emits a diagnostic JSON. It measures, it does not fix.
* `TTExposureMerge` (default output `merged_graded`) repairs roughly the first 12 frames of each new segment, interpolates a frame for same-shot first-frame jumps (hard cuts are left alone), then applies a bounded 0.75–1.30 gain against the first segment's median luma and pins the 10 frames before each boundary. It is slow and re-encodes with libx264 crf19.

## Requirements

**ComfyUI core**: must include the official MiniMax H3 nodes and model (`comfy_extras/nodes_minimax_h3.py`, `comfy/ldm/minimax/`). Developed and verified on **ComfyUI 0.39.0**. Also uses `comfy_api`, `comfy.nested_tensor` and `comfy_extras.nodes_audio.vae_decode_audio`.

**Python packages** (`requirements.txt`):

| Package | Used by | Behavior when missing |
| --- | --- | --- |
| `torchaudio` | `AudioToAudioPath` wav output | Running that node raises |
| `imageio-ffmpeg` | one ffmpeg source for merging | Falls back to `FFMPEG_PATH` / `bin/ffmpeg.exe` / system PATH |

torch / numpy / Pillow / aiohttp / av / safetensors already ship with ComfyUI.

**Optional plugin**: `TTFANHyperVAE2xChunked` loads `h3_t8/hyper_vae_2x.py` from `comfyui-minimax-h3-audio-T8`. Without it the node is simply not registered.

## Installation

### Method 1: manual (standard)

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/canyuevip/ComfyUI_TT_MiniMaxH3.git
pip install -r ComfyUI_TT_MiniMaxH3/requirements.txt
```

Restart ComfyUI; the nodes live under the `TT_MiniMaxH3` categories. This edition downloads no models and installs no external runtime — it runs offline straight after installation.

### Method 2: ComfyUI Manager

1. Open ComfyUI Manager
2. Install via Git URL
3. Enter `https://github.com/canyuevip/ComfyUI_TT_MiniMaxH3.git`
4. Restart ComfyUI

## External resources

| Resource | Location | How to get it |
| --- | --- | --- |
| H3 weights / VAE / CLIP | `models/diffusion_models/`, `models/vae/`, `models/text_encoders/` | From the official MiniMax H3 releases; this plugin never downloads the main model |

ffmpeg lookup order: `FFMPEG_PATH` -> plugin `bin/ffmpeg.exe` -> `imageio-ffmpeg` -> system PATH. No ffmpeg binary is shipped.

## Quick start (talking head)

1. Load a stock H3 chain: UNet + CLIP Loader (`type=minimax`) + `video_vae` + `audio_vae`.
2. Produce the segment JSON with your own speech-recognition step, in the format shown above.
3. Add `TTAudioChainConsole`: paste the segment JSON (or convert that input and link it), write the shared prompt, confirm context frames and canvas size.
4. Add `TTH3AudioConditioning` in lock-source mode with the console's per-segment audio, plus the character first frame.
5. Decode the sampler's joint AV latent with `TTH3AVDecode`. Sampler settings follow the official MiniMax H3 recommendations; this plugin does not override sampler defaults.
6. For multi-segment continuity add the save / load / motion-context / trim quartet, or let the console advance the indices for you.
7. Click **▶ start** in the panel; `TTVideoMerge` produces `merged.mp4` after the last segment.

## Example workflows

`example_workflows/` reserves three planned chains (single-segment talking head, multi-segment chained run, seam inspection and repair). **No workflow JSON is included yet** — see [example_workflows/README.md](example_workflows/README.md) for the plan and conventions.

## Troubleshooting

* **Old category gone?** The plugin was renamed from `H3工具` to `TT_MiniMaxH3` and node IDs from `H3Kit*` to `TT*`; reconnect old workflows.
* **Where does the segment JSON come from?** This edition ships no speech-recognition node: paste JSON produced by your own speech recognition into the console's **segment JSON** box, or provide it from a string node or a WhisperX-style chain of your own. `AudioToAudioPath` hands `AUDIO` to external tools.
* **Lip-sync drift or audio/video length mismatch?** Use `TTDurationCrop` to cut the generated segment to the source duration; it recomputes the frame rate so container length matches exactly.
* **Flash or color jump at a seam?** Locate the dimension with `TTSeamDoctor`, then merge with `TTExposureMerge`.
* **HyperVAE 2x node missing?** Install `comfyui-minimax-h3-audio-T8`; without it the node is skipped.

## Panel endpoints

The browser panels in `js/` use these local endpoints, same-origin with the ComfyUI server only — nothing leaves the machine:

| Endpoint | Purpose |
| --- | --- |
| `GET /tt/h3/image` | Absolute-path thumbnails in the multi image loader |
| `GET /tt/h3/audio` | Audio preview with Range support for scrubbing |
| `POST /tt/audio_chain/reset` | Stop the chain (bump generation, drop queued items) |
| `GET /tt/audio_chain/status` | Chain status and segment plan, used to restore the panel after a refresh |
| `POST /tt/motion_context/clear_latents` | Clear numbered `h3_context` slots |
| `POST /tt/video/merge` | Merge entry point for external scripts |

## Versioning and changelog

Current version: **v1.1.0** (2026-10-14). Full history lives in [CHANGELOG.md](CHANGELOG.md); packaged builds in [Releases](https://github.com/canyuevip/ComfyUI_TT_MiniMaxH3/releases).

Two editions, same codebase, same version number:

| Edition | Repository | Contents |
| --- | --- | --- |
| Public lite | this repository | 18 nodes, without the audio segmenter and the local Qwen prompt expander; no automatic downloads, no external runtime |
| Full | `ComfyUI_TT_MiniMaxH3-full` (private, access on request) | 20 nodes, including those two model-heavy ones |

## Credits

* [Comfy-Org/ComfyUI](https://github.com/Comfy-Org/ComfyUI) — official MiniMax H3 support and conditioning protocol
* [MiniMax-AI](https://github.com/MiniMax-AI) — MiniMax H3 model
* [NikoDemon80/ComfyUI-H3-Motion-Context](https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context) — origin of the `motion_ctx/` quartet

## License

* This plugin is released under **GPL-3.0** (see [LICENSE](LICENSE)), © 2026 canyuevip.
* `motion_ctx/` is derived from **NikoDemon80/ComfyUI-H3-Motion-Context** (GPL-3.0, © 2026 NikoDemon80); the four base nodes are kept and wired into the chained-run system.
