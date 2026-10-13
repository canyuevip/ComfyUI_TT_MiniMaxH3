"""Pin previous-clip motion at the head of an H3 clip.

Wire it between a stock H3 conditioning node and the sampler:

    MiniMaxH3ImageToVideo / MiniMaxH3ReferenceToVideo (or the t2v path)
        -> H3 Motion Context
        -> guider / sampler

Two axes to test, both cheap.

encode_mode
  frames  one VAE call per frame, each pinned as its own cond block. The
          model sees N snapshots at N instants.
  video   one VAE call for the whole run. The H3 video VAE has latent_dim
          3, so it reads the batch axis as time and compresses the run
          into fewer latent steps (5 pixel frames -> 2 steps, 22 -> 7).
          Each step becomes one cond block, so the motion between frames
          lives inside the latent instead of being implied across separate
          stills. Far fewer rows and one VAE load.

anchor_mode
  head    pinned frames occupy indices 0..N-1 of the delivered timeline.
          They come back in the output, so trim that many frames off the
          front before concatenating.
  before  pinned frames sit at negative indices, ending at -1, so
          delivered frame 0 continues from them and nothing is wasted.
          Their time coordinates land below text_len, which is the range
          the text rows occupy. Whether that collision matters is exactly
          what this mode is asking.
"""

import gc
import logging
import os
import re

import comfy.utils
import folder_paths
import node_helpers
import torch

try:
    from safetensors.torch import load_file as _st_load, save_file as _st_save
except ImportError:  # ComfyUI always ships safetensors; belt and braces
    _st_load = _st_save = None

from .layout_contract import ensure as _ensure_layout_contract
from .layout_contract import is_checked as _layout_checked

try:
    import torchaudio
except ImportError:
    torchaudio = None

_LOG = logging.getLogger("h3_motion_context")


def _ensure_layout_ok():
    """Prove ComfyUI still places anchors the way this pack needs, once.

    This used to install two runtime patches. ComfyUI 0.33 does natively
    what they existed to do, so the node now builds plain keyframe dicts
    and only has to check that the arithmetic behind them still holds. See
    layout_contract.py for what is checked and why it is not free.

    Run on first use rather than at import, same as the patches were: the
    pack sitting in custom_nodes should change nothing at all until you
    actually chain a clip. The cost is that a failure shows up on the
    first render instead of in the startup log.
    """
    _ensure_layout_contract("pinning a clip")


FRAME_PER_TOKEN = (1, 4, 4, 4, 4)
FPS = 24  # H3's native rate; audio latents run at 40 Hz, hence FRAME_RESCALE 5/3
FRAME_RESCALE = 5.0 / 3.0
AUDIO_HZ = 40.0

# Run lengths the video VAE's downscale formula max(1, (n - 5) // 17 * 5 + 2)
# actually distinguishes. Anything between two grid points encodes to the same
# number of latent steps as the lower one, but the steps then cover the FIRST
# `covered` frames of the input rather than the last: encoding 10 frames yields
# the same 2 steps as encoding 5, representing frames [-10..-6] of the source
# clip instead of [-5..-1]. The pinned run would end five frames early and the
# delivered clip would continue from the wrong instant. So off-grid requests
# are snapped DOWN before slicing, keeping content and coverage in agreement.
# The grid is 17m+5 and continues upward; the node only offers up to 56,
# but the snap-down logic knows the higher points so an out-of-range
# request lands on the nearest real one instead of being clamped to 39.
VIDEO_RUN_GRID = (124, 107, 90, 73, 56, 39, 22, 5, 1)

# Settings that used to be widgets. Each had exactly one right answer, so
# offering the wrong one was noise. The losing branches are still in the
# code below: change a constant here to reproduce the failure they cause.
#
#   ENCODE_MODE   "video" encodes the pinned run in one VAE call, so the
#                 motion lives inside the latent. "frames" encodes each
#                 frame as its own still, costs twice the rows and left a
#                 visible seam in testing.
#   ANCHOR_MODE   "head" pins the run at the start of the clip, where the
#                 Trim node removes it. "before" places it at negative
#                 time so nothing needs trimming, but the coordinates
#                 collide with the text rows, which weakens the anchors
#                 and darkens the output.
#   AUDIO_MODE    "timeline" puts the pinned audio on this clip's own
#                 timeline so the model continues it. "ref" is the stock
#                 placement, which the model imitates instead: similar
#                 music, not the same recording, plus a tick at the join.
#   CROP          only ever applied when an aspect ratio changed between
#                 clips, which the resolution check now refuses outright.
ENCODE_MODE = "video"
ANCHOR_MODE = "head"
AUDIO_MODE = "timeline"
CROP = "disabled"


def _pixel_frames(latent_t):
    """Pixel frames covered by latent_t latent steps."""
    return sum(FRAME_PER_TOKEN[k % 5] for k in range(latent_t))


def _step_offsets(latent_t):
    """Pixel-frame index at which each latent step begins."""
    out, acc = [], 0
    for k in range(latent_t):
        out.append(acc)
        acc += FRAME_PER_TOKEN[k % 5]
    return out


def _resize(image, width, height, crop):
    # image [B, H, W, C] -> [B, height, width, 3]; matches the stock helper
    samples = image[..., :3].movedim(-1, 1)
    samples = comfy.utils.common_upscale(samples, width, height, "lanczos", crop)
    return samples.movedim(1, -1)


def _encode_tail_audio(audio_vae, audio, seconds):
    """Encode the last `seconds` of a clip's audio with the H3 audio VAE.

    Returns ([1, 32, 2, T] latent, T) where T counts 40 Hz latent steps,
    matching what the layout calls ref_audio_t.
    """
    waveform = audio["waveform"]  # [B, C, L]
    sr = int(audio["sample_rate"])
    vae_sr = int(getattr(audio_vae, "audio_sample_rate", 32000))
    if sr != vae_sr:
        if torchaudio is None:
            raise RuntimeError(
                "h3_motion_context: context_audio is %d Hz but the VAE wants %d Hz "
                "and torchaudio is not available to resample." % (sr, vae_sr))
        waveform = torchaudio.functional.resample(waveform, sr, vae_sr)
    want = int(round(seconds * vae_sr))
    have = int(waveform.shape[-1])
    if have < want:
        _LOG.warning("h3_motion_context: context_audio is %.3fs, shorter than the "
                     "%.3fs of pinned video. Pinning what there is.",
                     have / vae_sr, seconds)
    else:
        waveform = waveform[..., have - want:]
    z = audio_vae.encode(waveform[:1].movedim(1, -1))  # [1, 32, 2, T]
    return z, int(z.shape[-1])


def _streams_from_latent(latent):
    """Unpack an H3 AV latent into its contained streams.

    NestedTensor.__getitem__ broadcasts the index into every contained
    tensor rather than selecting one, so samples[0] would strip the batch
    dimension off both streams. unbind() returns the pair.
    """
    samples = latent["samples"]
    if hasattr(samples, "unbind"):
        parts = list(samples.unbind())
    elif isinstance(samples, (tuple, list)):
        parts = list(samples)
    else:
        raise ValueError(
            "h3_motion_context: expected a MiniMax H3 AV latent (a nested "
            "video/audio pair), got %r" % type(samples))
    if not parts:
        raise ValueError("h3_motion_context: AV latent contains no streams")
    return parts


def _video_from_latent(latent):
    """Pull the video stream out of an H3 AV latent."""
    video = _streams_from_latent(latent)[0]
    if video.ndim == 4:  # unbatched [C,T,H,W]
        video = video.unsqueeze(0)
    if video.ndim != 5:
        raise ValueError("h3_motion_context: expected video latent [B,C,T,H,W], "
                         "got shape %s" % (tuple(video.shape),))
    return video


def _masks_from_latent(latent):
    """Split an H3 AV latent's noise_mask into (video_mask, audio_mask).

    Mirrors the official layout: a nested (video, audio) pair when both
    streams carry a mask, a bare tensor for a legacy video-only mask, and
    (None, None) when the latent has no mask at all. The audio mask is
    what carries a lock_source request downstream, so it has to survive
    into the guide latent instead of being replaced by an all-ones mask.
    """
    masks = latent.get("noise_mask") if isinstance(latent, dict) else None
    if masks is None:
        return None, None
    if getattr(masks, "is_nested", False) or hasattr(masks, "unbind"):
        parts = list(masks.unbind())
        if len(parts) >= 2:
            return parts[0], parts[1]
    if isinstance(masks, torch.Tensor):
        return masks, None
    return None, None


def _steps_for_frames(n):
    """Latent steps covering exactly n pixel frames from cycle position 0.

    Returns None when no whole number of steps covers n. The video VAE's
    steps alternate 1, 4, 4, 4, 4 pixel frames, so only certain totals are
    reachable: 1, 5, 9, ... and of the windows this node offers, 5, 22, 39
    and 56 land on 2, 7, 12 and 17 steps. The 1-frame window does not,
    because the last step of a clip spans 4 frames, not 1.
    """
    k, covered = 0, 0
    while covered < n:
        covered += FRAME_PER_TOKEN[k % 5]
        k += 1
    return k if covered == n else None


def _video_tail_from_latent(latent, n):
    """Slice the last n pixel frames of video straight out of a generated
    H3 latent, skipping the h264 decode and the VAE encode.

    Returns (blocks, offsets, covered) in the same shape the encode path
    produces, so everything downstream is unchanged.

    This is only sound because the tail window always starts at cycle
    position 0. A clip is 17g+5 frames, which is 5g+2 latent steps; the
    windows are 2, 7, 12 and 17 steps; and 5g+2 minus any of those is a
    multiple of 5. So the sliced run has the same 1, 4, 4, 4, 4 phase as a
    freshly encoded one and _step_offsets applies unchanged. Asserted
    below rather than assumed, because if it ever stopped holding the
    pinned content would silently disagree with the positions written for
    it and the join would land at the wrong instant.
    """
    video = _video_from_latent(latent)
    total = int(video.shape[2])
    steps = _steps_for_frames(n)
    if steps is None:
        raise ValueError(
            "h3_motion_context: a %d frame window is not a whole number of "
            "latent steps, so it cannot be sliced from a latent. Use 5, 22, "
            "39 or 56, or unwire context_latent to encode pixels." % n)
    if steps > total:
        raise ValueError(
            "h3_motion_context: asked for %d latent steps, context_latent "
            "has %d." % (steps, total))
    start = total - steps
    if start % 5 != 0:
        raise RuntimeError(
            "h3_motion_context: the %d step tail of a %d step latent starts "
            "at cycle position %d, not 0, so its frame spans would not match "
            "the positions written for them. Clip lengths are meant to make "
            "this impossible; refusing rather than rendering a shifted join."
            % (steps, total, start % 5))
    covered = _pixel_frames(steps)
    if covered != n:
        raise RuntimeError(
            "h3_motion_context: %d steps cover %d frames, expected %d."
            % (steps, covered, n))
    blocks = [video[:1, :, start + k:start + k + 1].clone()
              for k in range(steps)]
    return blocks, _step_offsets(steps), covered


def _audio_tail_from_latent(latent, a_frames):
    """Slice the last `a_frames` worth of audio steps straight out of a
    generated H3 latent, skipping the decode -> re-encode round trip.

    Returns (tail latent [1, C, 2, rt], rt, overhang) where rt counts
    40 Hz latent steps and overhang is the signed fraction of a step by
    which the clip's audio grid overshoots its last pixel frame.

    H3 rounds the audio grid to the NEAREST step, not up, so overhang is
    negative for a third of legal clip lengths. 5/3 of a frame count
    lands on .0, .333 or .667 and never on .5, so there are exactly three
    cases:

        frames % 3 == 0   243 wants 405.00, allocates 405, overhang    0
        frames % 3 == 1   124 wants 206.67, allocates 207, overhang +1/3
        frames % 3 == 2   260 wants 433.33, allocates 433, overhang -1/3

    A positive overhang means the latent's final step reaches past the
    last frame, a negative one means it stops short. Either way the
    caller compensates the placement with it, so the pinned content lands
    where its samples actually sit. The decoded-audio path never sees
    this because match_tail cuts at the frame.
    """
    parts = _streams_from_latent(latent)
    if len(parts) < 2:
        raise ValueError(
            "h3_motion_context: context_latent has no audio stream. Wire the "
            "sampler output of an H3 AV graph, not a video-only latent.")
    video, audio = parts[0], parts[1]
    if video.ndim == 4:
        video = video.unsqueeze(0)
    if audio.ndim == 3:  # unbatched [C,2,T]
        audio = audio.unsqueeze(0)
    if audio.ndim != 4:
        raise ValueError("h3_motion_context: expected audio latent [B,C,2,T], "
                         "got shape %s" % (tuple(audio.shape),))
    total_t = int(audio.shape[-1])
    frames = _pixel_frames(int(video.shape[2]))
    overhang = total_t - FRAME_RESCALE * frames
    # legal values are exactly 0, +1/3 and -1/3; the band is the widest
    # one that admits all three and still rejects a grid that is out by a
    # whole step or more
    if not (-0.5 < overhang < 0.5):
        _LOG.warning(
            "h3_motion_context: context_latent audio grid is unexpected "
            "(%d steps for %d frames); assuming no overhang.", total_t, frames)
        overhang = 0.0
    rt = int(round(a_frames / float(FPS) * AUDIO_HZ))
    if rt > total_t:
        _LOG.warning("h3_motion_context: asked for %d audio steps, the latent "
                     "has %d. Pinning all of it.", rt, total_t)
        rt = total_t
    if rt < 1:
        raise ValueError("h3_motion_context: audio window is empty")
    tail = audio[:1, ..., total_t - rt:].clone()
    return tail, rt, float(overhang)


def _crop_to_delivered(video, audio, target_seconds, fps):
    """Cut a loaded render back to the frames it actually delivered.

    A clip carries more than the delivery: a relay clip's frames start
    with the handoff head (the console shifted its audio window back by
    the context length, so the model rendered the overlap, and the Trim
    node cuts it off the front), and H3 rounds the total up onto its
    17m+5 grid, leaving 0-16 alignment frames on the tail. target_seconds
    is where the delivered content ENDS in the file -- segment length
    plus the head for a relay clip, the segment length alone for the
    first one (the chain fills it in). Pinning past that end hands the
    next clip content no viewer ever saw -- the last second of a render
    drifts (a push-in, or back toward the idle pose) precisely because it
    is being trimmed away -- and the join copies it: the next clip opens
    zoomed or frozen for a beat. Cropping to the largest end <= delivered
    with end % 17 == 5 keeps the 22-frame window a whole 7 steps, so the
    pinned run covers delivered frames only; the snap itself leaves at
    most 16 frames (0.67s) of the designed overlap at the join.

    Returns the streams unchanged when target_seconds is 0 (manually
    loaded files) or when there is nothing safe to cut.
    """
    if not target_seconds or float(target_seconds) <= 0.0:
        return video, audio
    delivered = int(round(float(target_seconds) * float(fps)))
    end = delivered - ((delivered - 5) % 17)
    total = _pixel_frames(int(video.shape[2]))
    if end < 22 or end >= total:
        return video, audio
    steps = _steps_for_frames(end)
    audio_steps = int(round(end / float(fps) * AUDIO_HZ))
    _LOG.info("h3_motion_context: cropping loaded latent to its delivered %d "
              "of %d frames; the context window pins frames %d..%d instead "
              "of the undelivered tail.", end, total, end - 22, end - 1)
    return (video[:, :, :steps].contiguous(),
            audio[..., :audio_steps].contiguous())


# ---- Context softening: perturb the pinned tail ------------------------------
# A chain that hands the model the previous clip's tail unchanged makes it
# copy its own (already compressed) output forward, so detail and identity
# decay a few clips in. Adding deterministic Gaussian noise -- heavily on the
# older steps, barely on the last ones -- makes the model redraw the body
# against a fuzzy boundary while the join still lands on phase. The tail is
# the trimmed head, so nothing noisy is ever delivered. These are the
# validated numbers.
CONTEXT_NOISE_ALPHA_END = 0.10
CONTEXT_NOISE_RAMP_STEPS = 2
CONTEXT_NOISE_SEED = 0x48334833   # fixed: the same chain reproduces itself


def _soften_context_blocks(blocks, alpha, seed=CONTEXT_NOISE_SEED):
    """Perturb the pinned context tail before handing it over.

    ``alpha`` is the noise fraction on the older steps; the newest
    ``CONTEXT_NOISE_RAMP_STEPS`` steps ramp down to ``CONTEXT_NOISE_ALPHA_END``
    so the boundary keeps its phase. ``alpha <= 0`` returns the blocks as-is.
    """
    if not blocks or float(alpha) <= 0.0:
        return blocks
    tail = torch.cat([blk for blk in blocks], dim=2)
    scale = float(tail.detach().float().std().item())
    if not (scale > 0.0):
        scale = 1.0
    generator = torch.Generator(device="cpu")
    generator.manual_seed(int(seed) & 0xFFFFFFFFFFFFFFFF)
    steps = int(tail.shape[2])
    ramp = min(CONTEXT_NOISE_RAMP_STEPS, steps)
    out = tail.clone()
    for index in range(steps):
        from_end = steps - 1 - index
        if from_end >= ramp:
            amount = float(alpha)
        else:
            amount = float(alpha) + (CONTEXT_NOISE_ALPHA_END - float(alpha)) * (
                ramp - from_end) / float(ramp)
        if amount <= 0.0:
            continue
        target = out[:, :, index]
        noise = torch.randn(
            target.shape, generator=generator, device="cpu",
            dtype=torch.float32,
        ).mul_(scale).to(device=target.device, dtype=target.dtype)
        out[:, :, index] = target * (1.0 - amount) + noise * amount
    return [out[:, :, index:index + 1].clone() for index in range(steps)]


# ---- Open-head handoff: leave the head to be redrawn -------------------------
# Instead of pinning the whole head as keyframes (which makes the model copy
# its own already compressed output forward and decay), the head is SEEDED into
# the target latent and left denoisable, so the model re-draws it; only the
# boundary tokens are pinned, which is what holds the join's phase and motion
# direction. The audio overlap is pinned in full. That redraw is what breaks
# the recursive degradation a long chain builds up.
HANDOFF_KEYFRAME = "钉死头部"
HANDOFF_OPEN = "开放头部"
HANDOFF_MODES = (HANDOFF_KEYFRAME, HANDOFF_OPEN)
# 旧工作流里存的是带括注的旧名，执行前归一，免得老流程图被前端当成非法值重置。
HANDOFF_LEGACY = {"钉死头部（旧）": HANDOFF_KEYFRAME, "开放头部（重画）": HANDOFF_OPEN}
GUIDE_BOUNDARY_TOKENS = 2


def _pack_guide_latent(video, audio, video_mask, audio_mask):
    try:
        from comfy.nested_tensor import NestedTensor
    except Exception:
        NestedTensor = None
    if NestedTensor is not None:
        return {"samples": NestedTensor((video, audio)),
                "noise_mask": NestedTensor((video_mask, audio_mask))}
    return {"samples": (video, audio), "noise_mask": (video_mask, audio_mask)}


def _build_guide_latent(latent, video_blocks, audio_tail, boundary_tokens):
    """Seed the head into the target latent and pin only the boundary tokens."""
    video, audio = _streams_from_latent(latent)
    if video.ndim == 4:
        video = video.unsqueeze(0)
    video = video.clone()
    audio = audio.clone()

    head = torch.cat([blk for blk in video_blocks], dim=2)
    steps = min(int(head.shape[2]), int(video.shape[2]))
    if steps > 0:
        video[:, :, :steps] = head[:, :, :steps].to(device=video.device, dtype=video.dtype)

    video_mask = torch.ones(
        (video.shape[0], 1, video.shape[2], video.shape[3], video.shape[4]),
        device=video.device, dtype=video.dtype)
    pin = min(max(0, int(boundary_tokens)), steps)
    if pin > 0:
        video_mask[:, :, steps - pin:steps] = 0.0

    # Carry the audio mask forward from the wired latent instead of
    # rebuilding it. A lock_source upstream writes an all-zero audio mask
    # to keep the original voice; an all-ones mask here would silently
    # discard that and let the model regenerate the speech, which is
    # exactly the "wrong audio" failure. Open-head mode should only change
    # how the video head is handled, not what happens to the sound.
    _, up_audio_mask = _masks_from_latent(latent)
    audio_mask = torch.ones(
        (audio.shape[0], 1, audio.shape[2], audio.shape[3]),
        device=audio.device, dtype=audio.dtype)
    if isinstance(up_audio_mask, torch.Tensor):
        # The mask arrives in whatever shape its producer emitted: the
        # stock Set Latent Noise Mask re-shapes its input to [B, 1, H, W],
        # so a plain 64x64 SolidMask reaches us as [1, 1, 64, 64] rather
        # than the audio latent's [B, 32, 2, T]. Normalise it exactly the
        # way the sampler will (comfy.utils.reshape_mask), then collapse
        # the channel axis so it matches this node's [B, 1, stereo, T]
        # audio mask. A mask that means "lock the whole stream" (all
        # zeros) then survives the carry -- copying it in raw would only
        # cover its literal width and crash on the shape mismatch -- and
        # a mask whose dimensions were already right passes through
        # unchanged.
        m = up_audio_mask.to(device=audio.device, dtype=audio.dtype)
        m = comfy.utils.reshape_mask(m, audio.shape)
        if m.shape[1] != 1:
            m = m.amax(dim=1, keepdim=True)
        audio_mask = m[:, :1, :, :]
    # Only seed the previous clip's tail sound when the model is allowed to
    # regenerate audio. A locked stream (all-zero mask, e.g. lock_source
    # passthrough) carries the drive recording verbatim: overwriting its head
    # would replace the first second of real speech with the previous clip's
    # tail, which survives the trim and ships as garbled, out-of-sync sound.
    audio_locked = not bool(torch.count_nonzero(audio_mask).item())
    if audio_locked and isinstance(audio_tail, torch.Tensor) and audio_tail.ndim == 4:
        _LOG.info("h3_motion_context: open-head handoff: audio stream is locked "
                  "(passthrough); leaving it untouched instead of seeding the "
                  "previous clip's tail.")
    if isinstance(audio_tail, torch.Tensor) and audio_tail.ndim == 4 and not audio_locked:
        count = min(int(audio_tail.shape[-1]), int(audio.shape[-1]))
        if count > 0:
            audio[:, :, :, :count] = audio_tail[..., :count].to(
                device=audio.device, dtype=audio.dtype)
            audio_mask[:, :, :, :count] = 0.0

    return _pack_guide_latent(video, audio, video_mask, audio_mask)


class TTH3MotionContext:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "conditioning": ("CONDITIONING",),
                "vae": ("VAE",),
                "latent": ("LATENT",),
                "context_length": (["22", "5", "39", "56"], {
                    "default": "22",
                    "tooltip": "Frames of the previous clip's picture to "
                               "carry over. Only these lengths are whole "
                               "numbers of latent steps, so only these are "
                               "offered. 5 is just barely fluid, 22 is "
                               "nearly seamless. Longer windows pin more "
                               "motion but come off the front of the "
                               "delivered clip, so 56 spends 2.3 seconds of "
                               "the render on frames you throw away."}),
                "audio_context_length": ("INT", {
                    "default": 24, "min": 0, "max": 240,
                    "tooltip": "Frames of tail audio to pin, independent of "
                               "the picture window. 0 follows it. The window "
                               "is END-aligned with the pinned video, so "
                               "this only controls how far back the sound "
                               "reaches. Multiples of 3 land exactly on the "
                               "40 Hz audio grid and multiples of 24 are "
                               "whole seconds: 24 pins the last second. "
                               "Off-grid values are widened to the nearest "
                               "whole step."}),
                "soften_alpha": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01,
                    "tooltip": "上下文软化强度（默认关闭）。大于 0 时把钉给模型的上一段尾部"
                               "掺入该比例的噪声，让它重画边界。实测在 22 帧上下文下会把运动线索"
                               "一起抹掉，导致画面发白、僵成一张图。除非专门调参，保持 0。"}),
                "衔接方式": (list(HANDOFF_MODES), {
                    "default": HANDOFF_KEYFRAME,
                    "tooltip": "钉死头部：把上一段尾部当作硬关键帧钉住，机位最稳，但模型会"
                               "照抄自己、长链越跑越糊。开放头部：把尾部塞进目标 latent "
                               "的头部并留作可重画（模型重画头部），只钉住边界 2 个 token，"
                               "从而打断递归劣化。要生效还需把采样的 latent 改接本节点的 latent 输出。"}),
            },
            "optional": {
                "context_frames": ("IMAGE", {
                    "tooltip": "Decoded frames of the previous clip. Used "
                               "when no context_latent is wired. When one "
                               "is, the picture comes from it instead and "
                               "this is ignored."}),
                "context_latent": ("LATENT", {
                    "tooltip": "Previous clip's SAMPLER OUTPUT latent (the "
                               "same one you wire into the decode nodes). "
                               "Supplies both picture and sound, sliced "
                               "straight out, skipping the decode and "
                               "re-encode that cost a little quality at "
                               "every link of a chain. Must be the same "
                               "resolution as the clip being generated."}),
                "audio_vae": ("VAE", {
                    "tooltip": "H3 audio VAE. Supply with context_audio to "
                               "carry the previous clip's tail sound across "
                               "the join. Not needed when context_latent is "
                               "wired."}),
                "context_audio": ("AUDIO", {
                    "tooltip": "Audio of the previous clip. The tail "
                               "matching the pinned frames is encoded and "
                               "pinned alongside them. Ignored when "
                               "context_latent is wired."}),
            },
        }

    RETURN_TYPES = ("CONDITIONING", "INT", "LATENT")
    RETURN_NAMES = ("conditioning", "trim_frames", "latent")
    FUNCTION = "apply"
    CATEGORY = "TT_MiniMaxH3/生成串联"
    DESCRIPTION = ("Pin a run of consecutive frames from a previous clip as "
                   "never-denoised conditioning rows, so the model reads real "
                   "motion instead of guessing it from a single still. With "
                   "context_latent wired, both picture and sound are sliced "
                   "from the previous clip's latent, skipping the decode and "
                   "re-encode that cost a little quality at every link. A "
                   "last_frame anchor on the incoming conditioning is kept "
                   "and pinned alongside the head. FAN addition: "
                   "tiling_tiles_out auto-aligns with the FAN SelfLift Avatar "
                   "high-resolution tiling (same pixel>=1024 rule), and "
                   "status_text reports the current tiling state. soften_alpha "
                   "is off by default; when raised it perturbs the pinned tail "
                   "with noise, which can wash out the motion cue and freeze the "
                   "clip, so leave it at 0 unless tuning.")

    @staticmethod
    def _auto_tiling(px_h, px_w):
        """Replicate FAN SelfLift Avatar's auto tiling rule exactly.
        Same threshold and tiles formula so upstream and downstream never
        disagree: any side >= 1024px enables tiling, tiles = clamp(2..4,
        ceil(long_side / 1024)).
        """
        import math
        if px_h >= 1024 or px_w >= 1024:
            long_side = max(px_h, px_w)
            tiles = max(2, min(4, math.ceil(long_side / 1024)))
            return True, tiles
        return False, 1

    def apply(self, conditioning, vae, latent, context_length,
              audio_context_length=24, soften_alpha=0.0,
              context_frames=None, context_latent=None, audio_vae=None,
              context_audio=None, 衔接方式=HANDOFF_KEYFRAME):
        # ---- FAN addition: tolerate a list-wrapped latent from tiling ----
        # FAN SelfLift Avatar with tiling enabled may hand this node a
        # top-level list of latents. Unwrap to a plain LATENT dict here so
        # the rest of the original logic is unchanged.
        if isinstance(latent, list):
            latent = latent[0]
        if isinstance(context_latent, list):
            context_latent = context_latent[0]

        # ---- FAN addition: read resolution first so we can report tiles ----
        # even on the no-context early return path.
        _v = _video_from_latent(latent)
        _w = int(_v.shape[4]) * 16
        _h = int(_v.shape[3]) * 16
        _no_ctx = context_latent is None and context_frames is None
        _use_tiling, _tiles_n = self._auto_tiling(_h, _w)
        if _no_ctx:
            return (conditioning, 0, latent)

        encode_mode, anchor_mode = ENCODE_MODE, ANCHOR_MODE
        audio_mode, crop = AUDIO_MODE, CROP
        衔接方式 = HANDOFF_LEGACY.get(str(衔接方式), 衔接方式)
        handoff_open = str(衔接方式) == HANDOFF_OPEN
        context_length = int(context_length)
        _ensure_layout_ok()

        video = _v
        latent_t = int(video.shape[2])
        width = _w
        height = _h
        frame_count = _pixel_frames(latent_t)
        # ---- FAN addition: auto tiling, aligned with SelfLift Avatar ----
        # h3_tiling.py is patched to cut refs/cond_video_latents together
        # with the latent tiles, so chained clips can use normal tiling.
        use_tiling, tiles_n = _use_tiling, _tiles_n
        _tiling_status = (f"FAN tiling {'ON' if use_tiling else 'OFF'} | "
                          f"tiles={tiles_n} | {width}x{height}px")

        # Decide where the pinned VIDEO comes from before anything else,
        # because it decides how many frames are even available. Slicing it
        # out of the previous clip's latent removes an h264 decode and a
        # VAE encode from the path, and the blocks come out bit-identical
        # to what the model produced rather than a reconstruction of it.
        # A wired latent supplies the picture as well as the sound: the
        # pinned blocks are then exactly the steps the model produced,
        # with no h264 decode, no resize and no VAE round trip to shift
        # colour or contrast. Frames are the path when no latent is wired.
        if context_latent is not None:
            src_video = _video_from_latent(context_latent)
            src_w = int(src_video.shape[4]) * 16
            src_h = int(src_video.shape[3]) * 16
            if src_w != width or src_h != height:
                # a latent cannot be resized. Falling back to frames here
                # would quietly take the lossy path on a graph the user
                # thinks is fine, and a resolution change mid-chain is
                # nearly always a mistake, so say so instead.
                raise ValueError(
                    "h3_motion_context: context_latent is %dx%d but this "
                    "clip is %dx%d. A latent cannot be resized, so the "
                    "previous clip has to be regenerated at this "
                    "resolution, or the chain restarted here."
                    % (src_w, src_h, width, height))
            if int(src_video.shape[1]) != int(video.shape[1]):
                raise ValueError(
                    "h3_motion_context: context_latent has %d channels, "
                    "this clip has %d. That is not an H3 video latent from "
                    "the same model."
                    % (int(src_video.shape[1]), int(video.shape[1])))
            available = _pixel_frames(int(src_video.shape[2]))
            video_src = "latent"
        else:
            available = int(context_frames.shape[0])
            video_src = "pixels"

        n = min(int(context_length), available)
        if n < 1:
            raise ValueError("h3_motion_context: no frames available to pin")
        if n < context_length:
            _LOG.warning("h3_motion_context: only %d frames available, pinning %d",
                         available, n)

        if encode_mode == "video":
            # snap down to the VAE grid BEFORE slicing, so the frames encoded
            # are exactly the frames the latent steps will cover (see
            # VIDEO_RUN_GRID). Slicing the last n and letting the VAE keep the
            # first `covered` of them would pin a run ending before the clip
            # does, and the join would jump by the difference.
            run = next(g for g in VIDEO_RUN_GRID if g <= n)
            if run != n:
                _LOG.warning(
                    "h3_motion_context: %d frames is off the VAE grid; pinning "
                    "the last %d instead (usable runs: 1, 5, 22, 39, 56)", n, run)
            n = run

        if n >= frame_count:
            raise ValueError(
                "h3_motion_context: asked to pin %d frames into a %d frame clip. "
                "The pinned run must be a small fraction of the timeline."
                % (n, frame_count))

        if video_src == "latent" and _steps_for_frames(n) is None:
            # every window the node offers is a whole number of steps, so
            # reaching this means the grid moved underneath us
            raise RuntimeError(
                "h3_motion_context: a %d frame window is not a whole number "
                "of latent steps. VIDEO_RUN_GRID no longer matches the "
                "VAE; refusing rather than rendering a shifted join." % n)

        if video_src == "latent":
            blocks, offsets, covered = _video_tail_from_latent(
                context_latent, n)
            span = covered
        else:
            # the LAST n frames of the incoming clip become the pinned run
            tail = _resize(context_frames[available - n:], width, height, crop)

        if video_src == "pixels" and encode_mode == "video":
            # one call; the VAE reads the batch axis as time and compresses
            enc = vae.encode(tail)
            if getattr(enc, "ndim", 0) != 5:
                raise ValueError(
                    "h3_motion_context: video-mode encode returned shape %s, "
                    "expected [B,C,T,H,W]. Try encode_mode=frames."
                    % (tuple(getattr(enc, "shape", ())),))
            steps = int(enc.shape[2])
            offsets = _step_offsets(steps)
            covered = _pixel_frames(steps)
            if covered != n:
                # n was snapped to the grid above, so a mismatch here means
                # the VAE's downscale formula changed underneath us and the
                # pinned content no longer lines up with the positions we
                # would write. Refuse rather than render a shifted join.
                raise RuntimeError(
                    "h3_motion_context: %d frames encoded to %d latent steps "
                    "covering %d frames; the VAE grid no longer matches "
                    "VIDEO_RUN_GRID. Upstream VAE change, refusing to run."
                    % (n, steps, covered))
            blocks = [enc[:, :, k:k + 1] for k in range(steps)]
            span = covered
        elif video_src == "pixels":
            blocks, offsets = [], []
            for i in range(n):
                blocks.append(vae.encode(tail[i:i + 1]))
                offsets.append(i)
            span = n

        # Soften the pinned tail so the model redraws the
        # boundary instead of copying its own output forward. See
        # _soften_context_blocks for why and for the taper.
        if not handoff_open:
            blocks = _soften_context_blocks(blocks, soften_alpha)

        if anchor_mode == "before":
            indices = [o - span for o in offsets]
        else:
            indices = list(offsets)

        keyframes = []
        for p, blk in zip(indices, () if handoff_open else blocks):
            # Straight to stock. `resolved_frame_index` is the real pixel
            # frame the block sits at, which ComfyUI 0.33 accepts for any
            # value, so there is nothing to smuggle and nothing to rewrite
            # afterwards. One keyframe per latent step rather than a single
            # multi-step block: `indices` already carries each step's real
            # offset, which keeps the encode_mode=frames path (offsets
            # 0..n-1, one still each) on exactly the same code. Stock lays
            # a 1-step latent at cursor + FRAME_RESCALE * index either way,
            # so the two forms produce identical rows.
            keyframes.append({
                "resolved_frame_index": p,
                "latent": blk,
            })

        ref_audio_t = 0
        audio_ref = None
        audio_kf = None
        audio_latent = None
        audio_end_frame = None
        a_frames = 0
        audio_src = "off"
        if context_latent is not None or context_audio is not None:
            # the audio window is independent of the video one: audio cond
            # rows cost rows but never cost delivered frames
            a_frames = int(audio_context_length) or span
            if context_latent is not None:
                if context_audio is not None:
                    _LOG.info("h3_motion_context: both context_latent and "
                              "context_audio wired; using the latent (skips "
                              "one VAE round trip).")
                audio_latent, ref_audio_t, overhang = _audio_tail_from_latent(
                    context_latent, a_frames)
                audio_src = "latent"
            else:
                if audio_vae is None:
                    raise ValueError(
                        "h3_motion_context: context_audio supplied without "
                        "audio_vae. Wire the H3 audio VAE, or wire "
                        "context_latent instead.")
                audio_latent, ref_audio_t = _encode_tail_audio(
                    audio_vae, context_audio, a_frames / float(FPS))
                overhang = 0.0  # decoded audio was match_tail-cut at the frame
                audio_src = "vae"
            if audio_mode == "timeline":
                # end-align the audio window with the pinned video: both are
                # the tail of clip A, so both must end at the same instant
                # of the new timeline, frame `span` in head mode (where
                # A's last frame sits), frame 0 in before mode. On the
                # latent path the sliced content overshoots A's last
                # frame by `overhang` of a step, signed, because H3
                # rounds its audio grid to the nearest step and so falls
                # short as often as it reaches past. The end coordinate
                # moves by exactly that much, and a keyframe index is a
                # plain multiplier so it takes a fractional frame.
                end_frame = float(span if anchor_mode == "head" else 0)
                end_frame += overhang / FRAME_RESCALE
                # then snap the window onto the target's own audio grid.
                # The end coordinate is FRAME_RESCALE * end_frame, and
                # FRAME_RESCALE is 5/3, so unless that product happens to
                # be a whole number the pinned rows land between the
                # integer coordinates the target's audio rows occupy. A
                # third of a step is 8.3 ms, which is the size of the
                # constant late offset measured on chained clips. Whether
                # it lands on or off the grid depends on the window
                # length, the path, and the clip's own grid overhang, so
                # it cycles rather than staying put. Rounding the end
                # coordinate to the nearest integer costs at most a third
                # of a step of placement and puts the pinned content on
                # the same grid as the sound being generated from it.
                end_coord = round(FRAME_RESCALE * end_frame)
                end_frame = end_coord / FRAME_RESCALE
                # Stock places a keyframe's audio window STARTING at
                # FRAME_RESCALE * index past the target origin and running
                # forward. We need it to END at the join, so the index is
                # the start of a window `ref_audio_t` steps wide:
                #
                #   start coord = FRAME_RESCALE * end_frame - ref_audio_t
                #   index       = end_frame - ref_audio_t / FRAME_RESCALE
                #
                # which is fractional, and negative whenever the window is
                # longer than the pinned head, which it normally is. Both
                # are legal arithmetic in the layout and neither is
                # reachable through the stock Add Guide node, so
                # layout_contract checks them before the first render.
                audio_kf = {
                    "resolved_frame_index": (end_frame
                                             - ref_audio_t / FRAME_RESCALE),
                    "audio_latent": audio_latent,
                }
                audio_end_frame = end_frame
            else:
                # stock reference placement: the window sits in its own
                # span ahead of the target, which is what makes the model
                # imitate the sound rather than continue it. Kept as the
                # comparison the timeline mode is measured against.
                audio_ref = {
                    "kind": "audio",
                    "ref_audio_t": ref_audio_t,
                    "audio_latent": audio_latent,
                }

        if handoff_open:
            # The audio overlap rides in on the latent mask below, not as a keyframe.
            audio_kf = None

        # MERGE with any keyframes already on the conditioning instead of
        # replacing them. A last_frame anchor from the upstream node, or an
        # Add Guide anchor, is a legitimate companion to a chained head: the
        # pinned run decides how the clip starts, the anchor decides where
        # it ends. They need no special handling now that every keyframe
        # carries its real index, ours included, and stock compensates all
        # of them for references the same way.
        #
        # Anchors inside the pinned head are dropped: the pinned run
        # already decides those frames, and a second cond block at the
        # same coordinate would fight it.
        # In keyframe mode the pinned run decides frames 0..span-1, so an
        # upstream frame-0 anchor (the reference image) is dropped as a
        # duplicate. In open mode nothing decides those frames, so it is kept:
        # it is the clip's identity anchor.
        head_end = span if (anchor_mode == "head" and not handoff_open) else 0
        tail_kfs = [audio_kf] if audio_kf is not None else []
        out = []
        dropped = []
        for emb, extra in conditioning:
            d = extra.copy()
            prior = d.get("minimax_keyframes") or []
            kept = []
            for kf in prior:
                p = kf.get("resolved_frame_index", 0)
                if p >= frame_count:
                    raise ValueError(
                        "h3_motion_context: the conditioning carries a "
                        "keyframe anchored at frame %s, but this clip is "
                        "only %d frames. Wire the conditioning and the "
                        "latent from the same node." % (p, frame_count))
                if p < head_end:
                    dropped.append(p)
                    continue
                kept.append(dict(kf))
            d["minimax_keyframes"] = kept + keyframes + tail_kfs
            out.append([emb, d])
        if dropped:
            _LOG.warning(
                "h3_motion_context: dropped %d keyframe anchor(s) at "
                "frame(s) %s: the pinned head already decides frames "
                "0..%d. A last_frame anchor is kept.",
                len(dropped), sorted(set(dropped)), head_end - 1)

        if audio_ref is not None:
            out = node_helpers.conditioning_set_values(
                out, {"minimax_refs": [audio_ref]}, append=True)

        trim = span if anchor_mode == "head" else 0
        _LOG.info("h3_motion_context: video from %s, %s/%s, %d frames -> %d "
                  "cond blocks at indices %d..%d, %d frame clip at %dx%d, "
                  "trim %d, audio %s",
                  video_src, encode_mode, anchor_mode, n, len(blocks),
                  indices[0], indices[-1], frame_count, width, height, trim,
                  ("%d frames -> %d latent steps (%.3fs) from %s, %s"
                   % (a_frames, ref_audio_t, ref_audio_t / AUDIO_HZ, audio_src,
                      "on the timeline ending at frame %.3f" % audio_end_frame
                      if audio_end_frame is not None
                      else "stock ref placement"))
                  if ref_audio_t else "off")
        guide_latent = latent
        if handoff_open:
            guide_latent = _build_guide_latent(latent, blocks, audio_latent,
                                               GUIDE_BOUNDARY_TOKENS)
            _LOG.info("h3_motion_context: open-head handoff: head seeded into "
                      "the latent, %d boundary token(s) pinned, rest left "
                      "denoisable so the model redraws it.",
                      GUIDE_BOUNDARY_TOKENS)
        return (out, trim, guide_latent)


class TTH3MotionContextTrim:
    """Drop the pinned head off a decoded clip, picture and sound together.

    The pinned frames occupy the start of the delivered timeline, so they
    have to come off before concatenating. Trimming only the images would
    leave the audio a full trim_frames longer than the video, and muxing
    those puts the whole soundtrack ahead of the picture by trim_frames/24
    seconds. At 5 frames that is 208ms, silent on ambience but squarely
    offbeat on anything with a pulse.

    So this takes both streams and removes the same span from each: whole
    frames from the images, the matching number of samples from the
    waveform. Wire trim_frames from the motion context node so the count
    follows whatever the encoder actually produced.

    The tail needs the same treatment for a different reason. H3's audio
    latent runs at 40 Hz against 24 fps picture, and FRAME_RESCALE is 5/3,
    so the grid rarely lands on a frame boundary. It rounds to the
    NEAREST step, which means a clip ships either about 8.3 ms more sound
    than picture or about 8.3 ms less, depending on its length:

        frames % 3 == 0   243 wants 405.00 steps, gets 405, exact
        frames % 3 == 1   124 wants 206.67 steps, gets 207, sound is long
        frames % 3 == 2   260 wants 433.33 steps, gets 433, sound is short

    Either way the error compounds. Concatenate two clips and the second
    seam is out by 16.7 ms, three and it is 25 ms, and it grows without
    bound down a chain. It reads as a faint dampening at the first join
    and a short click at later ones. Matching the tail to exactly
    frames/fps stops it accumulating: a long tail is truncated, a short
    one is zero-padded. The padded samples are sound the model never
    generated, so silence is the only honest fill.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "trim_frames": ("INT", {"default": 0, "min": 0, "max": 4096}),
            },
            "optional": {
                "audio": ("AUDIO", {
                    "tooltip": "Decoded audio for the same clip. Trimmed by the "
                               "matching duration so sound stays locked to "
                               "picture. Leave unwired for silent clips."}),
                "target_seconds": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 86400.0, "step": 0.01,
                    "tooltip": "Wanted delivered length in seconds. After the "
                               "pinned head comes off the front, drop whatever "
                               "is left past this length off the TAIL, so the "
                               "clip ships exactly target_seconds x fps frames "
                               "and the audio matches it. H3 rounds the frame "
                               "count up to the 17m+5 grid, so every clip "
                               "carries 0-16 frames (up to 0.67s) of tail it "
                               "did not ask for; wire the segment length here "
                               "to take that off in the same pass as the "
                               "pinned head. 0 disables the tail cut."}),
                "fps": ("FLOAT", {
                    "default": 24.0, "min": 1.0, "max": 240.0, "step": 0.001,
                    "tooltip": "Frame rate used to convert the trim into an "
                               "audio duration. Must match what you feed "
                               "Create Video."}),
                "match_tail": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Make the audio duration equal frames/fps "
                               "exactly, trimming a long tail or padding a "
                               "short one with silence. H3 rounds its audio "
                               "grid to the nearest step, so each clip "
                               "carries about 8ms too much or too little "
                               "sound, which accumulates at every join in a "
                               "chain."}),
            },
        }

    RETURN_TYPES = ("IMAGE", "AUDIO")
    RETURN_NAMES = ("images", "audio")
    FUNCTION = "trim"
    CATEGORY = "TT_MiniMaxH3/生成串联"
    DESCRIPTION = ("Remove the leading pinned frames from a decoded H3 clip, "
                   "trimming picture and sound by the same duration. With "
                   "target_seconds wired it also drops the alignment frames "
                   "H3 leaves on the tail, so one node removes both kinds of "
                   "surplus and the clip ships the exact length you asked for.")

    def trim(self, images, trim_frames, audio=None, fps=24.0, match_tail=True,
             target_seconds=0.0):
        n = max(0, int(trim_frames))
        total = int(images.shape[0])
        if n >= total:
            raise ValueError(
                "h3_motion_context: asked to trim %d frames from a %d frame clip"
                % (n, total))
        out_images = images[n:] if n else images
        if target_seconds and float(target_seconds) > 0:
            # H3 rounds the frame count up onto the 17m+5 grid, so the tail
            # carries 0-16 frames beyond what was asked for. Take those off in
            # the same pass as the pinned head: front = context frames, tail =
            # alignment frames. Cutting the alignment frames off the front
            # instead would eat real content and shift the join.
            want = max(1, int(round(float(target_seconds) * float(fps))))
            if want < int(out_images.shape[0]):
                _LOG.info("h3_motion_context: tail trimmed %d frames so the clip "
                          "delivers %.3fs", int(out_images.shape[0]) - want,
                          float(target_seconds))
                out_images = out_images[:want]

        out_audio = audio
        if audio is not None:
            waveform = audio["waveform"]
            sr = int(audio["sample_rate"])
            seconds = n / float(fps)
            cut = int(round(seconds * sr))
            length = int(waveform.shape[-1])
            if cut >= length:
                raise ValueError(
                    "h3_motion_context: trimming %.3fs from %.3fs of audio would "
                    "leave nothing. Check that fps matches the clip."
                    % (seconds, length / sr))
            waveform = waveform[..., cut:]

            if match_tail:
                frames_left = int(out_images.shape[0])
                want = int(round(frames_left / float(fps) * sr))
                have = int(waveform.shape[-1])
                if have > want:
                    over = have - want
                    waveform = waveform[..., :want]
                    _LOG.info("h3_motion_context: tail trimmed %d samples "
                              "(%.2fms) so audio matches %d frames exactly",
                              over, over / sr * 1000.0, frames_left)
                elif have < want:
                    # H3 rounds to the nearest audio step, so a third of
                    # clip lengths ship slightly LESS sound than picture
                    # rather than more. The missing samples are sound
                    # that was never generated, so zero is the honest
                    # fill; anything else would fabricate or attenuate
                    # real content to hide a seam. Leaving it short
                    # instead drifts every later clip earlier, and unlike
                    # the long case that error compounds down the chain.
                    # This also restores what the vae path assumes when
                    # it sets overhang to 0.
                    missing = want - have
                    waveform = torch.nn.functional.pad(waveform,
                                                       (0, missing))
                    _LOG.info("h3_motion_context: tail padded %d zero "
                              "samples (%.2fms) so audio matches %d "
                              "frames exactly",
                              missing, missing / sr * 1000.0, frames_left)

            out_audio = {"waveform": waveform, "sample_rate": sr}
            _LOG.info("h3_motion_context: %d frames / %.4fs picture, %.4fs sound, "
                      "drift %.2fms",
                      total - n, (total - n) / float(fps),
                      int(waveform.shape[-1]) / sr,
                      abs((total - n) / float(fps) - int(waveform.shape[-1]) / sr) * 1000.0)
        elif n:
            _LOG.info("h3_motion_context: trimmed %d leading frames, %d remain. "
                      "No audio wired; if this clip has sound, mux it through "
                      "this node or it will run %.3fs ahead of the picture.",
                      n, total - n, n / float(fps))

        return (out_images, out_audio)


def _under_output(path):
    """Resolve `path` inside ComfyUI's output folder. None if it would escape.

    Absolute paths are allowed only when they already live under output/.
    Relative paths join onto output/. This is what keeps latent_path from
    becoming an arbitrary file read or a delete-anywhere on Clear.
    """
    root = os.path.realpath(folder_paths.get_output_directory())
    p = (path or "").strip().strip('"').strip("'") or "h3_context"
    resolved = os.path.realpath(p if os.path.isabs(p) else os.path.join(root, p))
    if resolved != root and not resolved.startswith(root + os.sep):
        return None
    return resolved


def _resolve_latent_path(path, clip_index=0):
    """Turn the loader's path input into a concrete file under output/.

    Accepts a path relative to ComfyUI's output folder, an absolute path
    that already lives there, or a directory in either form. For a
    directory, clip_index must be a positive slot: clip 1 is
    *_00001.safetensors. Auto-mode files carry a trailing underscore
    (*_00001_.safetensors) and are never matched, because their numbers
    count runs and could hold a reject. clip_index 0 is handled by the
    Load node itself (no file, first clip).
    """
    c = _under_output(path)
    if not c:
        raise FileNotFoundError(
            "h3_motion_context: path must stay inside the ComfyUI output folder.")
    if os.path.isfile(c):
        return c
    if os.path.isdir(c):
        idx = int(clip_index)
        if idx <= 0:
            raise FileNotFoundError(
                "h3_motion_context: clip_index 0 does not load a file.")
        # indexed slots use the natural name: clip 2 lives in
        # *_00002.safetensors. Auto-mode files carry a trailing
        # underscore (*_00002_.safetensors) and are deliberately
        # NOT matched: their numbers count runs, not clips, so a
        # reject could be sitting in any of them.
        endings = ("_%05d.safetensors" % idx,
                   "_clip%03d.safetensors" % idx)  # older versions
        files = [os.path.join(c, f) for f in os.listdir(c)
                 if f.endswith(endings)]
        if not files:
            near = [f for f in os.listdir(c)
                    if f.endswith("_%05d_.safetensors" % idx)]
            hint = ""
            if near:
                hint = (" Found %s, which is an auto-numbered save "
                        "(trailing underscore = numbered by RUN, so "
                        "it may be a reject). If it really is clip "
                        "%d, rename it to drop the trailing "
                        "underscore: %s" %
                        (near[0], idx,
                         near[0].replace("_%05d_" % idx,
                                         "_%05d" % idx)))
            raise FileNotFoundError(
                "h3_motion_context: no saved latent for clip %d "
                "(no *_%05d.safetensors in %s).%s"
                % (idx, idx, c, hint))
        return max(files, key=os.path.getmtime)
    raise FileNotFoundError(
        "h3_motion_context: %r is neither a file nor a folder under the "
        "ComfyUI output directory." % ((path or "").strip() or "h3_context"))


def _latent_folder(path):
    """Directory that holds this chain's slot files, or None.

    Refuses anything outside ComfyUI's output folder so Clear cannot
    delete numbered safetensors elsewhere on disk.
    """
    c = _under_output(path)
    if not c:
        return None
    if os.path.isdir(c):
        return c
    if os.path.isfile(c):
        return os.path.dirname(c)
    return None


_CHAIN_SLOT_FILE = re.compile(
    r"(?:_\d{5}|_\d{5}_|_clip\d{3})\.safetensors(?:\.tmp)?$"
)


def _clear_clip_slots(latent_path):
    """Delete numbered chain slots. Custom filenames are left alone."""
    folder = _latent_folder(latent_path)
    if not folder:
        return 0
    removed = 0
    for name in os.listdir(folder):
        if not _CHAIN_SLOT_FILE.search(name):
            continue
        fp = os.path.join(folder, name)
        try:
            os.remove(fp)
        except OSError:
            gc.collect()
            os.remove(fp)
        removed += 1
    if removed:
        _LOG.info("h3_motion_context: cleared %d chain slot(s) from %s",
                  removed, folder)
    return removed


def register_chain_routes():
    """POST clear_latents for the chain consoles' Clear button.

    The path lives under /tt/ so it stays in this plugin's own namespace
    rather than shadowing the upstream /h3_motion_context/* handlers this was
    ported from.
    """
    try:
        from aiohttp import web
        from server import PromptServer
    except ImportError:
        return
    server = getattr(PromptServer, "instance", None)
    if server is None or getattr(register_chain_routes, "_done", False):
        return

    from .csrf_guard import require_same_origin

    @server.routes.post("/tt/motion_context/clear_latents")
    @require_same_origin
    async def _clear_latents_route(request):
        data = await request.json()
        n = _clear_clip_slots(data.get("latent_path") or "h3_context")
        return web.json_response({"removed": n})

    register_chain_routes._done = True


def _write_safetensors(path, tensors):
    # safetensors load_file memory-maps on Windows. Overwriting a mapped
    # slot (re-roll, or Load 0 aimed at the file Save is about to replace)
    # fails with os error 1224. Write a sibling temp file and replace.
    tmp = path + ".tmp"
    try:
        _st_save(tensors, tmp,
                 metadata={"format": "h3_motion_context_av_v1"})
        try:
            os.replace(tmp, path)
        except OSError:
            gc.collect()
            os.replace(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


class TTH3MotionContextSaveLatent:
    """Save an H3 AV latent to disk so the NEXT run can load it.

    Wiring the sampler's output straight into context_latent is a cycle:
    the sampler would be consuming its own result. The latent that motion
    context needs is the PREVIOUS clip's, which lives in the previous run
    -- so it has to cross runs through disk, the same way the frames and
    audio already do. Stock Save/Load Latent can't serialise H3's nested
    video/audio pair; this saves the two streams side by side.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "latent": ("LATENT", {
                    "tooltip": "The sampler's output latent (the same one "
                               "you wire into the decode nodes)."}),
                "filename_prefix": ("STRING", {
                    "default": "h3_context/clip",
                    "tooltip": "Saved under the ComfyUI output folder. The "
                               "default keeps all chain latents in one "
                               "folder so the Load node can always pick "
                               "the newest."}),
                "clip_index": ("INT", {
                    "default": 1, "min": 0, "max": 9999, "step": 1,
                    "tooltip": "Which clip of the chain THIS is. Saves to "
                               "that clip's fixed slot, so a re-roll "
                               "overwrites its own reject instead of "
                               "stacking new files. First clip: 1 here "
                               "and 0 on the Load node. Clip 2: 2 here "
                               "and 1 on the Load node. 0 = old behaviour, "
                               "a new numbered file every run (numbers "
                               "count runs, not clips)."}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("latent_path",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "TT_MiniMaxH3/生成串联"
    DESCRIPTION = ("Save the sampler's AV latent so the next run's Motion "
                   "Context node can pin audio from it via the matching "
                   "Load node.")

    def save(self, latent, filename_prefix, clip_index=0):
        if _st_save is None:
            raise RuntimeError("h3_motion_context: safetensors is not "
                               "available; cannot save latents.")
        parts = _streams_from_latent(latent)
        if len(parts) < 2:
            raise ValueError(
                "h3_motion_context: latent has no audio stream; wire the "
                "sampler output of an H3 AV graph.")
        video = parts[0].cpu().contiguous()
        audio = parts[1].cpu().contiguous()
        folder, filename, counter, _, _ = folder_paths.get_save_image_path(
            filename_prefix, folder_paths.get_output_directory())
        if int(clip_index) > 0:
            # fixed slot with the natural name: clip 2 -> *_00002. A
            # re-roll of this clip overwrites its own save, so rejects
            # never accumulate or get loaded later. Auto mode (below)
            # keeps a trailing underscore, which is what excludes its
            # run-numbered files from indexed loading.
            path = os.path.join(folder, "%s_%05d.safetensors"
                                % (filename, int(clip_index)))
        else:
            path = os.path.join(folder, "%s_%05d_.safetensors"
                                % (filename, counter))
        _write_safetensors(path, {"video": video, "audio": audio})
        _LOG.info("h3_motion_context: saved AV latent to %s (video %s, "
                  "audio %s)", path, tuple(video.shape), tuple(audio.shape))
        return (path,)


class TTH3MotionContextLoadLatent:
    """Load a saved H3 AV latent for the context_latent input.

    clip_index means exactly what it says: set it to the clip you want to
    CONTINUE FROM, and that clip's slot is loaded. Generating clip 2 from
    clip 1: Load node 1, Save node 2. Use H3 Motion Context Chain to
    advance the pair and queue: Approve walks forward, Run/Re-roll stays
    on the current slot (use it instead of ComfyUI's Run), Chain is
    Approve on a loop (at 0/1 with no clip 1 yet it generates that
    first), Reset sets Load 0 / Save 1, Clear latents deletes numbered
    slots. All three nodes must sit in the same canvas group.

    At 0 there is no previous clip: the loader returns nothing and Motion
    Context passes the conditioning through. First clip of a chain is
    Load 0 / Save 1, then Load 1 / Save 2, and so on.

    The output is ONLY for the Motion Context node's context_latent input.
    It is not a decodable latent -- do not wire it into VAE decode.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "latent_path": ("STRING", {
                    "default": "h3_context",
                    "tooltip": "A saved latent file, or a folder (relative "
                               "paths resolve against the ComfyUI output "
                               "directory). Pointing at a specific FILE "
                               "always loads that file when clip_index "
                               "is greater than 0, ignoring the index. "
                               "clip_index 0 never reads a file."}),
                "clip_index": ("INT", {
                    "default": 0, "min": 0, "max": 9999, "step": 1,
                    "tooltip": "The clip to CONTINUE FROM: that clip's "
                               "slot is loaded. First clip: 0 here and 1 "
                               "on the Save node (nothing is loaded). "
                               "Clip 2 from clip 1: 1 here and 2 on the "
                               "Save node."}),
                "independent_mode": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Independent / loop-but-no-resume: when on, "
                               "clip_index is ignored, no previous latent "
                               "is read, Motion Context passes through with "
                               "trim_frames=0, so every clip is an "
                               "independent t2v. Chain bookkeeping, Approve/"
                               "Re-roll and Save-to-numbered-slot still work."}),
            },
            "optional": {
                "target_seconds": ("FLOAT", {
                    "default": 0.0, "min": 0.0, "max": 86400.0, "step": 0.01,
                    "tooltip": "Where the loaded clip's delivered content "
                               "ENDS in the file, in seconds. A relay clip "
                               "(segment 2 on) starts with its handoff head "
                               "-- the context frames the console shifted the "
                               "audio back by, which the Trim node cuts off "
                               "the front -- so it ends at segment length + "
                               "context frames / fps; the first clip has no "
                               "head and ends at its segment length. Past "
                               "that point the render keeps the handoff "
                               "overshoot and up to 0.67s of grid alignment, "
                               "and that tail drifts (push-in, idle pose) "
                               "precisely because it is being trimmed away -- "
                               "pinning it into the next clip copies frames "
                               "no viewer ever saw. 0 loads the file "
                               "unchanged. The auto-chain fills this in for "
                               "every relayed segment, and the panel does the "
                               "same before a manual submit."}),
                "fps": ("FLOAT", {
                    "default": 24.0, "min": 1.0, "max": 240.0, "step": 0.001,
                    "tooltip": "Frame rate target_seconds is measured in. "
                               "Must match the render (H3 default 24)."}),
            },
        }

    RETURN_TYPES = ("LATENT", "INT")
    RETURN_NAMES = ("latent", "index")
    FUNCTION = "load"
    CATEGORY = "TT_MiniMaxH3/生成串联"
    DESCRIPTION = ("Load a latent saved by H3 Motion Context Save Latent, "
                   "for the context_latent input only. Flip independent_mode "
                   "off to chain clips, on to cycle through the same Chain "
                   "bookkeeping without carrying the previous clip over. "
                   "target_seconds crops the loaded render at the point where "
                   "its delivered content ends -- segment length plus the "
                   "relay handoff head, which the chain fills in; 0 loads "
                   "the file unchanged.")

    @classmethod
    def IS_CHANGED(cls, latent_path, clip_index=0, independent_mode=False,
                   target_seconds=0.0, fps=24.0):
        # Independent mode never touches disk, so never recompute because a
        # slot on disk changed behind its back.
        if independent_mode:
            return "disabled"
        # the path string stays constant while the file behind it changes
        # (an overwritten slot), so cache on the resolved file identity
        # instead -- otherwise ComfyUI would happily serve a stale latent
        # forever. Index 0 never reads a file. The crop length is part of
        # the output, so it is part of the key.
        if int(clip_index) <= 0:
            return "disabled"
        try:
            p = _resolve_latent_path(latent_path, clip_index)
            return "%s:%d:%s:%s:owned" % (p, os.stat(p).st_mtime_ns,
                                          float(target_seconds), float(fps))
        except Exception:
            return float("NaN")  # unresolvable: never cache

    def load(self, latent_path, clip_index=0, independent_mode=False,
             target_seconds=0.0, fps=24.0):
        index = int(clip_index)
        if independent_mode:
            # Do not read the previous clip at all: Motion Context receives
            # None, passes conditioning through and returns trim_frames=0.
            # Save still writes this clip's numbered slot, Chain still walks
            # the indices -- only the carry-over reference is skipped.
            _LOG.info(
                "h3_motion_context: independent_mode ON, ignoring clip_index "
                "%d (previous clip is NOT referenced).", index)
            return (None, index)
        if index <= 0:
            return (None, index)
        if _st_load is None:
            raise RuntimeError("h3_motion_context: safetensors is not "
                               "available; cannot load latents.")
        path = _resolve_latent_path(latent_path, clip_index)
        data = _st_load(path)
        if "video" not in data or "audio" not in data:
            raise ValueError(
                "h3_motion_context: %s is not an h3_motion_context latent "
                "(missing video/audio streams). Was it saved by the stock "
                "Save Latent node instead?" % path)
        # copy off the mmap so a later Save can overwrite this slot on Windows
        video = data.pop("video").contiguous().clone()
        audio = data.pop("audio").contiguous().clone()
        video, audio = _crop_to_delivered(video, audio, target_seconds, fps)
        _LOG.info("h3_motion_context: loaded AV latent from %s", path)
        # a plain list, not a NestedTensor: only this repo's context_latent
        # input accepts it, which is the point -- it cannot be mistaken
        # for a decodable latent without failing loudly downstream
        return ({"samples": [video, audio]}, index)
