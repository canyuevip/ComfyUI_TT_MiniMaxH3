# -*- coding: utf-8 -*-
"""TT 自带「H3 音视频条件」节点（口播锁源 / 参考生成）。

对齐 ComfyUI 原生 MiniMax H3 的条件协议（minimax_keyframes / minimax_refs /
noise_mask），并补上口播场景的关键能力——锁定源音：把源音频编码后的
音频 latent 直接写入 AV 潜空间的音频轨，音频 noise_mask 置 0 锁死，采样时
只生成视频、音频原样保留。

只覆盖 TT 用到的输入子集：首尾帧 / 参考图 / 参考视频 / 参考音频 / 源音频；
不含报告、语义桥、视觉标记、严格提示词标签等扩展。

依赖仅 ComfyUI 核心（comfy.*）+ torch + comfy_api，无第三方库。
"""

from __future__ import annotations

import math

import torch

import comfy.audio
import comfy.model_management
import comfy.nested_tensor
import comfy.utils
import node_helpers
from comfy_api.latest import io

CANVAS_MULTIPLE = 32
BASE_SHORT_EDGE = 768
FPS = 24
AUDIO_LATENT_FPS = 40


def _align_frames_up(length):
    """向上对齐 MiniMax H3 的 17n+5 帧网格。"""
    frame_count = max(5, int(length))
    return frame_count + ((5 - frame_count) % 17)


def _align_frames_down(frame_count):
    frame_count = int(frame_count)
    return frame_count - ((frame_count - 5) % 17)


def _video_latent_t(frame_count):
    return 2 if frame_count <= 5 else ((frame_count - 5) // 17) * 5 + 2


def _temporal_shape(length):
    frame_count = _align_frames_up(length)
    duration = frame_count / FPS
    return frame_count, _video_latent_t(frame_count), round(duration * AUDIO_LATENT_FPS)


def _adapt_canvas(width, height):
    ratio = width / height
    if ratio >= 1.0:
        nom_w, nom_h = BASE_SHORT_EDGE * ratio, BASE_SHORT_EDGE
    else:
        nom_w, nom_h = BASE_SHORT_EDGE, BASE_SHORT_EDGE / ratio
    return (max(CANVAS_MULTIPLE, round(nom_w / CANVAS_MULTIPLE) * CANVAS_MULTIPLE),
            max(CANVAS_MULTIPLE, round(nom_h / CANVAS_MULTIPLE) * CANVAS_MULTIPLE))


def _resize(image, width, height, crop):
    samples = image[..., :3].movedim(-1, 1)
    samples = comfy.utils.common_upscale(samples, width, height, "lanczos", crop)
    return samples.movedim(1, -1)


def _empty_av_latent(width, height, length):
    frame_count, latent_t, audio_t = _temporal_shape(length)
    device = comfy.model_management.intermediate_device()
    video = torch.zeros((1, 24, latent_t, height // 16, width // 16), device=device)
    audio = torch.zeros((1, 32, 2, audio_t), device=device)
    return {"samples": comfy.nested_tensor.NestedTensor((video, audio))}, frame_count


def _parts(av_latent):
    if not isinstance(av_latent, dict) or "samples" not in av_latent:
        raise ValueError("需要 MiniMax H3 联合 AV latent（视频+音频嵌套对）")
    samples = av_latent["samples"]
    if not getattr(samples, "is_nested", False):
        raise ValueError("需要嵌套的 MiniMax H3 视频/音频联合 latent")
    video, audio = tuple(samples.unbind())
    return video, audio


def _autogrow_values(inputs):
    if not inputs:
        return []

    def ordinal(item):
        try:
            return int(str(item[0]).rsplit("_", 1)[-1])
        except ValueError:
            return 10_000

    return [value for _, value in sorted(inputs.items(), key=ordinal) if value is not None]


def _encode_ref_audio(audio_vae, audio):
    """把 AUDIO 编码成 [1, 32, 2, T] 的 H3 音频 latent（40Hz latent 步）。"""
    waveform = audio["waveform"]  # [B, C, L]
    sample_rate = int(audio["sample_rate"])
    vae_sample_rate = int(getattr(audio_vae, "audio_sample_rate", 32000))
    if sample_rate != vae_sample_rate:
        waveform = comfy.audio.resample(waveform, sample_rate, vae_sample_rate)
    latent = audio_vae.encode(waveform[:1].movedim(1, -1))
    if not isinstance(latent, torch.Tensor) or latent.ndim != 4:
        raise ValueError("音频 VAE 未返回 [B, 32, 2, T] latent")
    return latent, int(latent.shape[-1])


def _fit_audio_latent(encoded_audio, template_audio):
    """把源音 latent 裁剪/补零到目标视频时长的音频轨长度。"""
    target_t = template_audio.shape[-1]
    if encoded_audio.shape[-1] > target_t:
        encoded_audio = encoded_audio[..., :target_t]
    elif encoded_audio.shape[-1] < target_t:
        padding = encoded_audio.new_zeros((*encoded_audio.shape[:-1], target_t - encoded_audio.shape[-1]))
        encoded_audio = torch.cat((encoded_audio, padding), dim=-1)
    return encoded_audio.to(device=template_audio.device, dtype=template_audio.dtype)


def _lock_audio(av_latent, encoded_audio):
    """把源音写入音频轨并锁死：音频 mask=0（不重生成），视频 mask=1（全生成）。"""
    video, template_audio = _parts(av_latent)
    fitted = _fit_audio_latent(encoded_audio, template_audio)
    output = av_latent.copy()
    output["samples"] = comfy.nested_tensor.NestedTensor((video, fitted))
    output["noise_mask"] = comfy.nested_tensor.NestedTensor((torch.ones_like(video), torch.zeros_like(fitted)))
    return output


class TTH3AudioConditioning(io.ComfyNode):
    """MiniMax H3 音视频条件：首尾帧 + 参考媒体 + 口播源音锁定。"""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="TTH3AudioConditioning",
            display_name="🎵H3音视频条件（口播/参考）",
            description="H3 原生的 T2VA/I2VA/FL2VA/Ref2VA 条件 + 口播锁源："
                        "锁定源音把「源音频」锁进音频轨（不重生成）；模型生成模式只让模型生成音频。"
                        "自包含实现，不依赖其它插件。",
            category="TT_MiniMaxH3/条件与解码",
            inputs=[
                io.Clip.Input("clip", display_name="CLIP", tooltip="MiniMax H3 的 Qwen3-VL CLIP。"),
                io.Vae.Input("video_vae", display_name="视频VAE", tooltip="MiniMax H3 视频 VAE。"),
                io.Vae.Input("audio_vae", display_name="音频VAE", tooltip="MiniMax H3 音频 VAE（锁源/参考音频必需）。"),
                io.String.Input("prompt", display_name="提示词", multiline=True, dynamic_prompts=True),
                io.Int.Input("width", display_name="宽度", default=1344, min=32, max=16384, step=32),
                io.Int.Input("height", display_name="高度", default=768, min=32, max=16384, step=32),
                io.Int.Input("length", display_name="帧数", default=124, min=5, max=None, step=17,
                             tooltip="24fps 帧数，自动向上对齐 17n+5 网格（124 ≈ 5.17 秒）。"),
                io.Combo.Input("audio_mode", display_name="音频模式", options=["锁定源音", "模型生成"], default="锁定源音",
                               tooltip="锁定源音：把源音频锁进音频轨，只生成视频；"
                                       "模型生成：音频由模型生成（忽略源音频的锁定，参考音频请走「参考音频」）。"),
                io.Audio.Input("source_audio", display_name="源音频", optional=True,
                               tooltip="口播/唱歌源音。锁定源音模式必连；模型生成模式仅透传到「输出音频」。"),
                io.Image.Input("first_frame", display_name="首帧", optional=True, tooltip="首帧（锚定第 0 帧）。"),
                io.Image.Input("last_frame", display_name="尾帧", optional=True, tooltip="尾帧（锚定末帧）。"),
                io.Combo.Input("ref_image_size", display_name="参考图尺寸",
                               options=["匹配幅面", "最大短边1536(1.5K)", "最大短边2048"],
                               default="匹配幅面", advanced=True,
                               tooltip="参考图尺寸：匹配幅面缩到生成画面的像素面积；最大短边1536(1.5K)/最大短边2048 把短边限制到对应像素，身份更稳但更慢。"),
                io.Autogrow.Input("ref_images", optional=True, display_name="参考图",
                                  template=io.Autogrow.TemplatePrefix(
                                      input=io.Image.Input("ref_image", display_name="参考图", tooltip="参考图（<Picture N> 引用）。"),
                                      prefix="ref_image_", min=0, max=9)),
                io.Autogrow.Input("ref_videos", optional=True, display_name="参考视频",
                                  template=io.Autogrow.TemplatePrefix(
                                      input=io.Image.Input("ref_video", display_name="参考视频", tooltip="参考视频（24fps IMAGE 批次，至少 5 帧，<Video N> 引用）。"),
                                      prefix="ref_video_", min=0, max=3)),
                io.Autogrow.Input("ref_audios", optional=True, display_name="参考音频",
                                  template=io.Autogrow.TemplatePrefix(
                                      input=io.Audio.Input("ref_audio", display_name="参考音频", tooltip="参考音频（音色/内容参考，<Audio N> 引用）。"),
                                      prefix="ref_audio_", min=0, max=3)),
            ],
            outputs=[
                io.Conditioning.Output(display_name="正向条件"),
                io.Latent.Output(display_name="AV潜空间"),
                io.Audio.Output(display_name="输出音频"),
            ],
        )

    @classmethod
    def execute(cls, clip, video_vae, audio_vae, prompt, width, height, length,
                audio_mode, source_audio=None, first_frame=None, last_frame=None,
                ref_image_size="匹配幅面", ref_images=None, ref_videos=None, ref_audios=None):
        if width % 32 or height % 32:
            raise ValueError("MiniMax H3 的宽度/高度必须能被 32 整除")
        if audio_mode == "锁定源音" and source_audio is None:
            raise ValueError("「锁定源音」模式需要连接「源音频」（口播源音）；"
                             "只想让模型生成音频请把「音频模式」改为「模型生成」")

        latent, frame_count = _empty_av_latent(width, height, length)

        keyframes = []
        keyframe_images = []
        if first_frame is not None:
            image = _resize(first_frame[:1], width, height, "disabled")
            keyframe_images.append(image)
            keyframes.append({"resolved_frame_index": 0, "latent": video_vae.encode(image)})
        if last_frame is not None:
            image = _resize(last_frame[:1], width, height, "center")
            keyframe_images.append(image)
            keyframes.append({"resolved_frame_index": frame_count - 1, "latent": video_vae.encode(image)})

        ref_items = []   # Qwen 侧呈现（<Picture N>/<Video N>/<Audio N>）
        ref_blocks = []  # DiT 侧条件块

        for image in _autogrow_values(ref_images):
            h, w = int(image.shape[1]), int(image.shape[2])
            if ref_image_size.startswith("最大短边"):
                cap = int(ref_image_size[len("最大短边"):].split("(")[0])
                scale = min(1.0, cap / min(w, h))
            else:
                scale = min(1.0, math.sqrt((width * height) / (w * h)))
            tw = max(CANVAS_MULTIPLE, round(w * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
            th = max(CANVAS_MULTIPLE, round(h * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
            resized = _resize(image[:1], tw, th, "disabled")
            ref_items.append({"type": "image", "data": resized})
            ref_blocks.append({"kind": "image", "latent_h": th // 16, "latent_w": tw // 16,
                               "latent": video_vae.encode(resized)})

        for frames in _autogrow_values(ref_videos):
            if frames.ndim != 4 or int(frames.shape[0]) < 5:
                raise ValueError("参考视频需要 24fps 的 IMAGE 批次且至少 5 帧")
            vh, vw = int(frames.shape[1]), int(frames.shape[2])
            cw, ch = _adapt_canvas(vw, vh)
            if vw * vh < cw * ch:
                cw = max(CANVAS_MULTIPLE, round(vw / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
                ch = max(CANVAS_MULTIPLE, round(vh / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
            frames = _resize(frames, cw, ch, "disabled")[:frame_count]
            count = _align_frames_down(int(frames.shape[0]))
            if count < 5:
                raise ValueError("参考视频对齐 17n+5 帧网格后不足 5 帧")
            frames = frames[:count]
            encoded = video_vae.encode(frames)
            sampled = list(range(0, count, FPS // 2))
            ref_items.append({"type": "video", "data": frames[sampled],
                              "timestamps": [index / FPS for index in sampled]})
            ref_blocks.append({"kind": "video", "latent_t": int(encoded.shape[2]),
                               "latent_h": ch // 16, "latent_w": cw // 16, "latent": encoded})

        for audio in _autogrow_values(ref_audios):
            encoded, audio_t = _encode_ref_audio(audio_vae, audio)
            ref_items.append({"type": "audio"})
            ref_blocks.append({"kind": "audio", "ref_audio_t": audio_t, "audio_latent": encoded})

        if audio_mode == "锁定源音":
            encoded_source, _ = _encode_ref_audio(audio_vae, source_audio)
            latent = _lock_audio(latent, encoded_source)

        if ref_blocks:
            items = [{"type": "image", "data": image} for image in keyframe_images] + ref_items
            tokens = clip.tokenize(prompt, minimax_ref_items=items)
        elif keyframe_images:
            tokens = clip.tokenize(prompt, images=keyframe_images)
        else:
            tokens = clip.tokenize(prompt)
        conditioning = clip.encode_from_tokens_scheduled(tokens)

        values = {}
        if keyframes:
            values["minimax_keyframes"] = keyframes
        if ref_blocks:
            values["minimax_refs"] = ref_blocks
        if values:
            conditioning = node_helpers.conditioning_set_values(conditioning, values)

        return io.NodeOutput(conditioning, latent, source_audio)
