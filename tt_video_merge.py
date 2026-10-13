"""视频合并：把同一目录下的分段视频（segNNN）按段号顺序拼成一个 MP4。

复用 tt_ffmpeg_utils 里的 ffmpeg 查找 / 分段收集 / 拼接实现，ffmpeg 优先用
插件目录 bin/ffmpeg.exe，不依赖外部环境。

给 🎬音频分段串联·一键生成 的连跑收尾用：节点面板跑完全部分段后，前端会调用
/tt/video/merge 把 output/<文件名前缀>/ 下的 seg* 拼起来，避免把合并塞进
「本节点执行时」而最后一段还没落盘。
"""

import asyncio
import os
import re

try:
    import folder_paths
except ImportError:
    folder_paths = None

from .tt_ffmpeg_utils import collect_segment_files, find_ffmpeg, merge_videos, prefix_to_segment_dir

MERGE_MODE_COPY = "直接拼接(不重编码)"
MERGE_MODE_REENCODE = "重编码(兼容模式)"
MERGE_MODE_EXPOSURE = "重编码+曝光锁定(防漂移)"
MERGE_MODES = [MERGE_MODE_COPY, MERGE_MODE_REENCODE]
# 控制台连跑收尾多出的档：合并时顺带做接缝修复 + 跨段曝光锁定（要逐段解码，慢）。
CHAIN_MERGE_MODES = MERGE_MODES + [MERGE_MODE_EXPOSURE]


def merge_segment_dir(dir_value, out_name="merged", reencode=False, ffmpeg_override="", expect=0):
    """合并 output/<dir_value>/seg* 分段视频，返回 dict。

    成功: {"ok": True, "filename", "subfolder", "path", "count"}
    失败: {"ok": False, "error"}
    expect > 0 时，收集到的分段数不足则不合并。
    相对路径按 🎬音频分段串联·一键生成 同样的规则净化成单个目录名（分隔符转 _）。
    """
    raw = prefix_to_segment_dir(dir_value)
    if not raw:
        return {"ok": False, "error": "未填写文件名前缀（分段视频所在目录）"}
    if os.path.isabs(raw) or re.match(r"^[A-Za-z]:[\\/]", raw):
        seg_dir = os.path.normpath(raw)
        subfolder = ""
    elif folder_paths is None:
        return {"ok": False, "error": "非 ComfyUI 环境，无法定位 output 目录"}
    else:
        folder = re.sub(r'[<>:"/\\|?*]', "_", raw)
        seg_dir = os.path.join(folder_paths.get_output_directory(), folder)
        subfolder = folder

    files = collect_segment_files(seg_dir)
    if len(files) < 2:
        return {"ok": False, "error": "未找到可合并的分段视频（%s，找到 %d 个）" % (seg_dir, len(files))}
    if expect and len(files) < expect:
        return {"ok": False, "error": "只找到 %d/%d 段，尚未全部生成" % (len(files), expect)}

    name = re.sub(r'[<>:"/\\|?*]', "_", str(out_name or "").strip()) or "merged"
    if not os.path.splitext(name)[1]:
        name += ".mp4"
    out_path = os.path.join(seg_dir, name)
    ffmpeg = str(ffmpeg_override or "").strip() or find_ffmpeg()
    ok, err = merge_videos(files, out_path, reencode=bool(reencode), ffmpeg=ffmpeg)
    if not ok:
        return {"ok": False, "error": err}
    return {"ok": True, "filename": name, "subfolder": subfolder, "path": out_path, "count": len(files)}


class TTVideoMerge:
    """把 output/<文件名前缀>/ 下的 segNNN 分段视频按顺序拼成一个 MP4。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "文件名前缀": ("STRING", {
                    "default": "h3_audio_chain",
                    "tooltip": "分段视频所在目录：相对 output/ 的子目录（与 🎬音频分段串联·一键生成 的"
                               "「文件名前缀」一致），也可以填绝对路径；直接接控制台的逐段"
                               "「文件名前缀」输出（基础名/segNNN）时自动按所在目录收集。",
                }),
                "合并输出名": ("STRING", {
                    "default": "merged",
                    "tooltip": "合并文件名（无需扩展名，自动补 .mp4），输出到分段所在的同一目录。",
                }),
                "合并方式": (MERGE_MODES, {
                    "default": MERGE_MODES[0],
                    "tooltip": "分段编码参数一致时用「直接拼接」秒级完成且无损失；参数不一致导致拼接失败时改用「重编码」。",
                }),
            },
            "optional": {
                "总段数": ("INT", {
                    "default": 0, "min": 0, "max": 9999,
                    "tooltip": "0 = 不校验，收集到几段就合并几段；填了段数时，不足则跳过不合并。",
                }),
                "ffmpeg路径": ("STRING", {
                    "default": "",
                    "tooltip": "留空自动查找：环境变量 FFMPEG_PATH → 插件 bin/ffmpeg.exe → imageio_ffmpeg → 系统 PATH。",
                }),
                "全部完成": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "接「🎬音频分段串联·一键生成」的「完成信号」：连跑还没跑到最后一段时为假，"
                               "本节点与后面的保存节点都会被跳过；最后一段为真时才真正合并。"
                               "不接线或保持默认 True 时每次都直接合并（旧工作流行为不变）。",
                }),
                "触发对齐": ("*", {
                    "forceInput": True,
                    "tooltip": "只为连线顺序：接视频保存节点（如 VideoCombine）的输出，保证本节点在"
                               "本段 MP4 落盘后才收集分段；运行时不读取这个值。",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "VIDEO")
    RETURN_NAMES = ("合并路径", "合并视频")
    FUNCTION = "merge"
    CATEGORY = "TT_MiniMaxH3/视频处理"
    OUTPUT_NODE = True
    DESCRIPTION = ("视频合并：把 output/<文件名前缀>/ 下的 segNNN 分段视频按段号顺序拼成一个 MP4"
                   "（默认 -c copy 不重编码）。可与「🎬音频分段串联·一键生成」共用同一个文件名前缀。"
                   "「全部完成」接串连控制台的「完成信号」后，连跑中途会跳过本节点、最后一段才合并；"
                   "「合并视频」输出可直接接 SaveVideo 等保存节点给平台 API 返回。")

    def merge(self, 文件名前缀, 合并输出名="merged", 合并方式=MERGE_MODES[0], 总段数=0,
              ffmpeg路径="", 全部完成=True, 触发对齐=None):
        from comfy_execution.graph import ExecutionBlocker
        from comfy_api.latest import InputImpl
        if not 全部完成:
            # 连跑还没到最后一段：拦住本节点和后面的保存节点，等「完成信号」打开再合并。
            return {"result": (ExecutionBlocker(None), ExecutionBlocker(None))}
        res = merge_segment_dir(
            文件名前缀, 合并输出名, 合并方式 == MERGE_MODE_REENCODE, ffmpeg路径, int(总段数 or 0))
        if not res.get("ok"):
            text = "❌ " + res.get("error", "合并失败")
            print("[TTVideoMerge] " + text)
            return {"ui": {"text": [text]}, "result": ("", ExecutionBlocker(None))}
        text = "✅ 已合并 %d 段 -> %s" % (res["count"], res["path"])
        print("[TTVideoMerge] " + text)
        return {"ui": {"text": [text]},
                "result": (res["path"], InputImpl.VideoFromFile(res["path"]))}


def register_video_merge_routes():
    """POST /tt/video/merge：给控制台面板跑完全部分段后触发的合并入口。"""
    try:
        from aiohttp import web
        from server import PromptServer
    except ImportError:
        return
    server = getattr(PromptServer, "instance", None)
    if server is None or getattr(register_video_merge_routes, "_done", False):
        return

    from .motion_ctx.csrf_guard import require_same_origin

    @server.routes.post("/tt/video/merge")
    @require_same_origin
    async def _merge_route(request):
        data = await request.json()
        res = await asyncio.to_thread(
            merge_segment_dir,
            data.get("dir"), data.get("out_name") or "merged",
            bool(data.get("reencode")), data.get("ffmpeg"),
            int(data.get("expect") or 0),
        )
        return web.json_response(res)

    register_video_merge_routes._done = True
