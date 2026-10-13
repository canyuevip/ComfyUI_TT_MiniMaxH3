# -*- coding: utf-8 -*-
"""「HyperVAE 2x 分帧加载」节点。

从已安装的 hyper_vae_2x.py 模块独立加载一份副本（与原模块内部那份互不干扰），
把其中的 pixel_shuffle_h3_images 替换为按 ps_chunk_frames 分帧循环版本；chunk 帧数
由节点参数控制，用于降低长视频解码 pixel_shuffle 的峰值显存。不修改原文件。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import torch
import torch.nn.functional as F

# hyper_vae_2x.py 文件路径（与本插件同在 custom_nodes 下）
_HYPERVAE_FILE = (
    Path(__file__).resolve().parent.parent
    / "comfyui-minimax-h3-audio-T8" / "h3_t8" / "hyper_vae_2x.py"
)

# 独立加载一份模块（与原模块内部那份互不干扰），patch 它自己的 pixel_shuffle
_spec = importlib.util.spec_from_file_location("tt_hypervae_2x_local", str(_HYPERVAE_FILE))
if _spec is None or _spec.loader is None or not _HYPERVAE_FILE.is_file():
    raise ImportError(f"无法加载 hyper_vae_2x: {_HYPERVAE_FILE}")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_orig_ps = _mod.pixel_shuffle_h3_images
_RGB_PHASES = _mod.RGB_PHASES


def _chunked_ps(images):
    chunk = int(getattr(_mod, "_tt_ps_chunk", 16))
    if chunk <= 0:
        chunk = 1
    if images.ndim != 5 or images.shape[-1] != 3 * _RGB_PHASES:
        return _orig_ps(images)
    batch, frames, height, width, _ = images.shape
    if frames <= chunk:
        return _orig_ps(images)
    # 预分配最终输出 [B,T,2H,2W,3]，逐块写入；不攒 list 再 cat，避免峰值翻倍
    out = torch.empty(
        (batch, frames, height * 2, width * 2, 3),
        dtype=images.dtype, device=images.device,
    )
    for start in range(0, frames, chunk):
        end = min(start + chunk, frames)
        blk = images[:, start:end]
        t = end - start
        packed = blk.permute(0, 1, 4, 2, 3).reshape(batch * t, 12, height, width)
        rgb = F.pixel_shuffle(packed, upscale_factor=2)
        rgb = rgb.reshape(batch, t, 3, height * 2, width * 2).permute(0, 1, 3, 4, 2)
        out[:, start:end] = rgb
    return out


# 替换模块里的函数（HyperVAE2x.decode / decode_tiled 调用的就是模块全局名）
_mod.pixel_shuffle_h3_images = _chunked_ps


class TTFANHyperVAE2xChunked:
    """加载 HyperVAE 2x，且最终 pixel_shuffle 按 ps_chunk_frames 分帧处理。"""

    @classmethod
    def INPUT_TYPES(cls):
        import folder_paths

        names = folder_paths.get_filename_list("vae")
        return {
            "required": {
                "vae_name": (names or ["填写绝对路径"], {
                    "tooltip": "列出 ComfyUI/models/vae 里的文件，选 HyperVAE 2x 权重。",
                }),
                "ps_chunk_frames": ("INT", {
                    "default": 16,
                    "min": 1,
                    "max": 124,
                    "step": 1,
                    "tooltip": "pixel_shuffle 一次处理几帧。内存紧调小(8/12)，内存宽裕调大(24/32)。",
                }),
            }
        }

    RETURN_TYPES = ("VAE", "STRING")
    RETURN_NAMES = ("video_vae", "report_json")
    FUNCTION = "load"
    CATEGORY = "TT_MiniMaxH3/视频处理"

    def load(self, vae_name, ps_chunk_frames):
        import folder_paths

        _mod._tt_ps_chunk = int(ps_chunk_frames)
        path = Path(folder_paths.get_full_path_or_raise("vae", vae_name))
        vae, report = _mod.load_hyper_vae_2x(path)
        report["ps_chunk_frames"] = int(ps_chunk_frames)
        report["chunked_pixel_shuffle"] = True
        return (vae, json.dumps(report, ensure_ascii=False, indent=2))
