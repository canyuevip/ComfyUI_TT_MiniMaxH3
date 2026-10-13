import os
import folder_paths

__version__ = "1.1.0"

try:
    import torchaudio
except ImportError:
    torchaudio = None

from .tt_h3_multi_image_loader import TTH3MultiImageLoader
from .tt_h3_multi_audio_loader import TTH3MultiAudioLoader
from .tt_duration_crop import TTDurationCrop
try:
    # 该节点独立加载 comfyui-minimax-h3-audio-T8 的 hyper_vae_2x 模块；未安装时跳过注册，不影响其它节点。
    from .tt_fan_hypervae_chunked import TTFANHyperVAE2xChunked
except ImportError as _exc:
    print(f"[TT] HyperVAE 2x 分帧加载节点未注册（需要 comfyui-minimax-h3-audio-T8 插件）: {_exc}")
    TTFANHyperVAE2xChunked = None
from .tt_group_switch import TTGroupSwitch
from .tt_video_merge import TTVideoMerge, register_video_merge_routes
from .tt_exposure_merge import TTExposureMerge
from .tt_seam_doctor import TTBridgeFrameGate, TTSeamDoctor
from .tt_h3_audio_conditioning import TTH3AudioConditioning
from .tt_h3_av_decode import TTH3AVDecode
from .motion_ctx import (
    TTH3MotionContext,
    TTH3MotionContextTrim,
    TTH3MotionContextSaveLatent,
    TTH3MotionContextLoadLatent,
)
from .motion_ctx.nodes import register_chain_routes
from .prompt_chain import (
    TTChainPromptLine,
    TTAudioChainConsole,
    register_audio_chain_routes,
)

class AudioToAudioPath:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"audio": ("AUDIO",)}}

    RETURN_TYPES = ("AUDIOPATH",)
    RETURN_NAMES = ("audio_path",)
    FUNCTION = "convert"
    CATEGORY = "TT_MiniMaxH3/音频处理"

    def convert(self, audio):
        if torchaudio is None:
            raise RuntimeError("AudioToAudioPath 需要 torchaudio（pip install torchaudio）")
        wf = audio["waveform"]          # (batch, channels, samples)
        if wf.dim() == 3:
            wf = wf[0]
        out_dir = os.path.join(folder_paths.get_temp_directory(), "whisperx_bridge")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "vocals_bridge.wav")
        torchaudio.save(path, wf.cpu(), int(audio["sample_rate"]))
        return (path,)

NODE_CLASS_MAPPINGS = {
    "TTH3MultiImageLoader": TTH3MultiImageLoader,
    "TTH3MultiAudioLoader": TTH3MultiAudioLoader,
    "TTH3MotionContext": TTH3MotionContext,
    "TTH3MotionContextTrim": TTH3MotionContextTrim,
    "TTH3MotionContextSaveLatent": TTH3MotionContextSaveLatent,
    "TTH3MotionContextLoadLatent": TTH3MotionContextLoadLatent,
    "TTChainPromptLine": TTChainPromptLine,
    "TTAudioChainConsole": TTAudioChainConsole,
    "TTDurationCrop": TTDurationCrop,
    "TTGroupSwitch": TTGroupSwitch,
    "TTVideoMerge": TTVideoMerge,
    "TTExposureMerge": TTExposureMerge,
    "TTBridgeFrameGate": TTBridgeFrameGate,
    "TTSeamDoctor": TTSeamDoctor,
    "TTH3AudioConditioning": TTH3AudioConditioning,
    "TTH3AVDecode": TTH3AVDecode,
    "AudioToAudioPath": AudioToAudioPath,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "TTH3MultiImageLoader": "🖼MiniMax H3 多图加载器",
    "TTH3MultiAudioLoader": "🔊MiniMax H3 多音频加载器",
    "TTH3MotionContext": "🎬H3运动上下文（续接上一段）",
    "TTH3MotionContextTrim": "✂H3运动上下文裁剪",
    "TTH3MotionContextSaveLatent": "💾H3上下文latent保存",
    "TTH3MotionContextLoadLatent": "📂H3上下文latent加载",
    "TTChainPromptLine": "📝逐段提示词取行",
    "TTAudioChainConsole": "🎬音频分段串联·一键生成",
    "TTDurationCrop": "⏱数字人/MV 时长裁剪",
    "TTGroupSwitch": "🎚编组开关（按数量）",
    "TTVideoMerge": "🎬视频合并（分段拼接）",
    "TTExposureMerge": "🎬视频合并（曝光锁定·接缝修复）",
    "TTBridgeFrameGate": "🧷桥帧门控（尾帧质量）",
    "TTSeamDoctor": "🩺接缝医生（五维z-score）",
    "TTH3AudioConditioning": "🎵H3音视频条件（口播/参考）",
    "TTH3AVDecode": "🎞H3音视频解码",
    "AudioToAudioPath": "Audio -> AudioPath (for WhisperX)",
}

if TTFANHyperVAE2xChunked is not None:
    NODE_CLASS_MAPPINGS["TTFANHyperVAE2xChunked"] = TTFANHyperVAE2xChunked
    NODE_DISPLAY_NAME_MAPPINGS["TTFANHyperVAE2xChunked"] = "🎞HyperVAE 2x 分帧加载"

WEB_DIRECTORY = "./js"

register_chain_routes()
register_video_merge_routes()
register_audio_chain_routes()

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
