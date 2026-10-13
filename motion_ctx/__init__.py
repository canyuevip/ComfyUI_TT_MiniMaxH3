"""H3 段间串联上下文，基于 NikoDemon80/ComfyUI-H3-Motion-Context（GPL-3.0）修改而来，
保留四个基础节点并接入本插件的一键连跑体系。

四个节点：主上下文 / 裁剪 / latent 保存 / latent 加载。用 ComfyUI 输出目录里的
safetensors 槽位在多次运行之间传递上一段的 AV latent，从而把上一段的画面尾巴和
声音尾巴钉到本段开头。串联的推进由「🎬音频分段串联·一键生成」的面板完成。
"""
from .nodes import (
    TTH3MotionContext,
    TTH3MotionContextTrim,
    TTH3MotionContextSaveLatent,
    TTH3MotionContextLoadLatent,
)

__all__ = [
    "TTH3MotionContext",
    "TTH3MotionContextTrim",
    "TTH3MotionContextSaveLatent",
    "TTH3MotionContextLoadLatent",
]
