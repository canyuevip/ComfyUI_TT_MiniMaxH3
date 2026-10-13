# -*- coding: utf-8 -*-
"""TT 自带「H3 音视频解码」节点。

把 MiniMax H3 的联合 AV 潜空间（视频+音频嵌套对）解码为图像帧与生成音频，
并把两路流拆成独立的视频/音频潜空间，供后续接缝处理与音频混合使用。

依赖仅 ComfyUI 核心（comfy.*）+ comfy_api + comfy_extras，无第三方库。
"""

from comfy_api.latest import io
from comfy_extras.nodes_audio import vae_decode_audio


def _split_av(av_latent):
    samples = av_latent.get("samples") if isinstance(av_latent, dict) else None
    if samples is None or not getattr(samples, "is_nested", False):
        raise ValueError("需要 MiniMax H3 联合 AV 潜空间（视频+音频嵌套对）")
    video, audio = tuple(samples.unbind())
    return video, audio

class TTH3AVDecode(io.ComfyNode):
    """MiniMax H3 联合 AV 潜空间解码：图像帧 + 生成音频 + 拆分潜空间。"""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="TTH3AVDecode",
            display_name="🎞H3音视频解码",
            description="把 MiniMax H3 的联合 AV 潜空间解码为图像帧与生成音频，"
                        "并按视频/音频拆出独立潜空间。自包含实现，不依赖其它插件。",
            category="TT_MiniMaxH3/条件与解码",
            inputs=[
                io.Latent.Input("av_latent", display_name="AV潜空间",
                                tooltip="采样器输出的 MiniMax H3 联合 AV 潜空间。"),
                io.Vae.Input("video_vae", display_name="视频VAE", tooltip="MiniMax H3 视频 VAE。"),
                io.Vae.Input("audio_vae", display_name="音频VAE", tooltip="MiniMax H3 音频 VAE。"),
            ],
            outputs=[
                io.Image.Output(display_name="图像"),
                io.Audio.Output(display_name="生成音频"),
                io.Latent.Output(display_name="视频潜空间"),
                io.Latent.Output(display_name="音频潜空间"),
            ],
        )

    @classmethod
    def execute(cls, av_latent, video_vae, audio_vae):
        video, audio = _split_av(av_latent)
        images = video_vae.decode(video)
        if images.ndim == 5:
            images = images.reshape(-1, *images.shape[-3:])
        decoded_audio = vae_decode_audio(audio_vae, {"samples": audio})

        video_latent = {key: value for key, value in av_latent.items() if key not in {"samples", "noise_mask"}}
        audio_latent = video_latent.copy()
        video_latent["samples"] = video
        audio_latent["samples"] = audio
        masks = av_latent.get("noise_mask")
        if getattr(masks, "is_nested", False):
            video_mask, audio_mask = masks.unbind()
            video_latent["noise_mask"] = video_mask
            audio_latent["noise_mask"] = audio_mask
        return io.NodeOutput(images, decoded_audio, video_latent, audio_latent)
