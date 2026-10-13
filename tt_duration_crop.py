# -*- coding: utf-8 -*-
"""「数字人/MV 时长裁剪」节点：把生成视频精确对齐到源音频时长（自包含实现）。

「启用裁剪」开关同时决定裁剪行为与输出音频源：
  - 开（裁剪模式）：视频精确对齐「裁剪模式音频」时长，输出音频 = 裁剪模式音频（源音频）
  - 关（透传模式）：图像/帧率原样，输出音频 = 透传模式音频（如 generated_audio）

背景：MiniMax H3 模型要求帧数必须是 5+17N（17 帧/块），生成侧的时长控制会把帧数
向上对齐，导致生成视频比音频长（如 10s 音频 -> 243 帧@24fps = 10.125s）。裁剪
模式下把帧数精确重采样回「目标时长 x 帧率」帧并重算 fps，使输出视频容器时长精确
等于目标音频时长。
"""

import torch


class TTDurationCrop:
    """数字人/MV 时长裁剪：开关同时切换音频源与裁剪行为。

    开 = 裁剪模式：按「裁剪模式音频」时长裁剪视频，输出音频 = 裁剪模式音频（源音频）。
    关 = 透传模式：图像/帧率原样，输出音频 = 透传模式音频（generated_audio）。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图像": ("IMAGE",),
                "帧率": ("FLOAT", {
                    "default": 24.0,
                    "min": 1.0,
                    "max": 120.0,
                    "step": 0.001,
                    "tooltip": "生成视频的实际帧率（MiniMax H3 通常为 24）。裁剪后会重算以保证时长精确",
                }),
                "启用裁剪": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "开=裁剪模式：对齐「裁剪模式音频」时长并输出该音频；关=透传模式：图像/帧率原样，输出「透传模式音频」",
                }),
            },
            "optional": {
                "裁剪模式音频": ("AUDIO",),
                "透传模式音频": ("AUDIO",),
                "目标秒数": ("FLOAT", {
                    "default": 0.0,
                    "min": 0.0,
                    "max": 86400.0,
                    "step": 0.01,
                    "tooltip": "手动指定目标时长（秒）；有「裁剪模式音频」时优先用音频时长",
                }),
            },
        }

    RETURN_TYPES = ("IMAGE", "FLOAT", "AUDIO")
    RETURN_NAMES = ("图像", "帧率", "音频")
    FUNCTION = "crop"
    CATEGORY = "TT_MiniMaxH3/视频处理"

    def crop(self, 图像, 帧率, 启用裁剪, 裁剪模式音频=None, 透传模式音频=None, 目标秒数=0.0):
        source_fps = max(0.001, float(帧率))
        valid_img = isinstance(图像, torch.Tensor) and 图像.ndim >= 1

        # 透传模式：图像/帧率原样，输出音频 = 透传模式音频（未接回退裁剪模式音频）
        if not bool(启用裁剪) or not valid_img:
            out_audio = 透传模式音频 if 透传模式音频 is not None else 裁剪模式音频
            return 图像, source_fps, out_audio

        # 裁剪模式：目标时长来自裁剪模式音频（或手动秒数）
        target_seconds = 0.0
        if 裁剪模式音频 is not None and isinstance(裁剪模式音频, dict) and "waveform" in 裁剪模式音频:
            waveform = 裁剪模式音频.get("waveform")
            sample_rate = float(裁剪模式音频.get("sample_rate") or 0)
            total_samples = int(waveform.shape[-1]) if waveform is not None and getattr(waveform, "ndim", 0) else 0
            if sample_rate <= 0 or total_samples <= 0:
                raise ValueError("裁剪模式音频时长读取失败：请检查 AUDIO 输入是否有效")
            target_seconds = total_samples / sample_rate
        elif 目标秒数 and 目标秒数 > 0:
            target_seconds = float(目标秒数)

        if target_seconds <= 0.0:
            out_audio = 透传模式音频 if 透传模式音频 is not None else 裁剪模式音频
            return 图像, source_fps, out_audio

        source_frames = int(图像.shape[0])
        if source_frames <= 0:
            out_audio = 透传模式音频 if 透传模式音频 is not None else 裁剪模式音频
            return 图像, source_fps, out_audio

        # 均匀重采样到目标帧数（首尾帧都保留），并重算 fps 使容器时长精确
        target_frames = max(1, int(round(target_seconds * source_fps)))
        if target_frames != source_frames:
            positions = torch.linspace(0, source_frames - 1, target_frames, device=图像.device)
            frame_indices = positions.round().to(dtype=torch.long)
            图像 = 图像.index_select(0, frame_indices)

        # 裁剪模式：输出音频 = 裁剪模式音频（源音频），未接回退透传模式音频
        out_audio = 裁剪模式音频 if 裁剪模式音频 is not None else 透传模式音频
        return 图像, float(target_frames / target_seconds), out_audio