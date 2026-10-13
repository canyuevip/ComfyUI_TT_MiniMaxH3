"""H3 Chain prompt line.

A multi-line prompt picker whose entry index follows the H3 Motion Context Chain
clip number automatically: chain segment 1 -> entry 0, segment 2 -> entry 1, ...

It does NOT call back into ComfyUI-H3-Motion-Context; the companion web extension
reads that plugin's Save Latent `clip_index` widget at queue time and writes our
`start_index` before the graph is serialized. Run/Re-roll does not move the
clip number, so it re-runs the same entry.

mode=lines     one prompt per line; `max_rows` lines per segment.
mode=segments  one prompt per `=== SEGMENT n | 8.0s ===` block, with the seconds
               from the block header emitted on the `duration` output. A plain
               `--- SEGMENT ---` header is accepted as well and yields 0 seconds.
"""

import json
import re

from . import auto_chain

_SEGMENT_HEAD = re.compile(r"^[ \t]*(?:={2,}|-{2,}|#{1,6}|\*{2,})[ \t]*SEGMENT\b[^\n]*$",
                           re.IGNORECASE | re.MULTILINE)
_SEGMENT_SECONDS = re.compile(r"\|\s*([0-9]+(?:\.[0-9]+)?)\s*s\b", re.IGNORECASE)


def _parse_segments(text):
    """Split SEGMENT-headed blocks into {prompt, duration} dicts, in file order."""
    marks = list(_SEGMENT_HEAD.finditer(text))
    segments = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        seconds = _SEGMENT_SECONDS.search(mark.group(0))
        segments.append({
            "prompt": text[mark.end():end].strip(),
            "duration": float(seconds.group(1)) if seconds else 0.0,
        })
    return segments


class TTChainPromptLine:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "dynamicPrompts": False,
                    "tooltip": "mode=lines：一行一条提示词；mode=segments：整段文本按 "
                               "=== SEGMENT n | 8.0s === 块划分，第 1 块给第 1 段用，"
                               "第 2 块给第 2 段用。",
                }),
                "start_index": ("INT", {
                    "default": 0, "min": 0, "max": 9999,
                    "tooltip": "取第几条（0 起）。串联运行时由本插件前端脚本自动跟随"
                               "「💾H3上下文latent保存 / 📂H3上下文latent加载」的段号，"
                               "一般无需手改；单独使用时可手动指定。",
                }),
                "max_rows": ("INT", {
                    "default": 1, "min": 1, "max": 9999,
                    "tooltip": "仅 mode=lines：每段输出几行；mode=segments 忽略。",
                }),
                "remove_empty_lines": ("BOOLEAN", {"default": True}),
                "mode": (["lines", "segments"], {
                    "default": "lines",
                    "tooltip": "lines：一行一条；segments：一块一条，并把块头的秒数"
                               "输出到「duration」上。",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "FLOAT")
    RETURN_NAMES = ("prompt", "duration")
    FUNCTION = "get_lines"
    CATEGORY = "TT_MiniMaxH3/提示词"
    DESCRIPTION = ("逐段提示词取行：第 N 段取第 N 条。mode=segments 时按 "
                   "=== SEGMENT === 块取段，并输出该块时长（秒）。")

    def get_lines(self, prompt, start_index, max_rows, remove_empty_lines=True, mode="lines"):
        text = prompt or ""
        if mode == "segments":
            segments = _parse_segments(text)
            if not segments:
                return ("", 0.0)
            index = max(0, min(int(start_index), len(segments) - 1))
            return (segments[index]["prompt"], float(segments[index]["duration"]))
        lines = text.split("\n")
        if remove_empty_lines:
            lines = [line for line in lines if line.strip()]
        if not lines:
            return ("", 0.0)
        start = max(0, min(int(start_index), len(lines) - 1))
        end = min(start + int(max_rows), len(lines))
        return ("\n".join(lines[start:end]), 0.0)


# 从第 2 段起追加到提示词末尾，告诉模型本段不是开头、要与上一段无缝接续。
_SEAMLESS_CONTINUE = (
    "【无缝衔接】这是同一段连续口播的第 {n} 段（共 {total} 段），不是视频开头，"
    "必须与上一段结尾无缝对接：从上一段最后一帧的人物姿态、口型、眼神、手势、景别、"
    "构图、光线和背景自然延续，段首不要重新起势、不要重新聚焦镜头、不要停顿或跳变；"
    "语速、语气、音量、音色与上一段保持一致，说话内容与上一段连贯。"
)


def _parse_plan(text):
    """解析分段JSON -> {"segments": [{start, duration, text}], "total", "full_text"}。

    接受 {"total": ..., "segments": [...]} 与裸数组两种结构；每项接受 start_time/start、
    duration、text/prompt。分段数、总时长、全文都从这份 JSON 里算出来，不需要额外输入。
    """
    raw = str(text or "").strip()
    if not raw:
        return {"segments": [], "total": 0.0, "full_text": ""}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("音频分段串联：「分段JSON」无法解析，请检查粘贴的内容或上游输出。") from exc
    items = (data.get("segments") if isinstance(data, dict) else data) or []
    segments = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start_time", item.get("start", 0.0)))
            duration = float(item.get("duration", 0.0))
        except (TypeError, ValueError):
            continue
        if duration <= 0:
            continue
        segments.append({"start": start, "duration": duration,
                         "text": str(item.get("text") or item.get("prompt") or "").strip()})

    total = 0.0
    full_text = ""
    if isinstance(data, dict):
        try:
            total = float(data.get("total") or 0.0)
        except (TypeError, ValueError):
            total = 0.0
        full_text = str(data.get("text") or data.get("full_text") or "").strip()
    if total <= 0:
        total = sum(item["duration"] for item in segments)
    if not full_text:
        full_text = " ".join(item["text"] for item in segments if item["text"])
    return {"segments": segments, "total": total, "full_text": full_text}


class TTAudioChainConsole:
    """一整个音频连跑的控制台。

    分段计划完全来自「分段JSON」：直接粘贴 JSON（外部语音识别、手工分段都行），
    或右键把「分段JSON」转换成输入接上游音频分段节点——本节点不绑定任何分段来源。
    工作流用 VHS_LoadAudioUpload 的 start_time / duration 截取音频的一段：本节点
    算出当前段的 start_time 与 duration，并把公共提示词与本段识别文字一并输出。
    从第 2 段起自动在提示词末尾追加无缝衔接说明，配合 H3 Motion Context 让本段与
    上一段自然接续。面板按钮会自动推进「当前段」，每次提交前同步 Motion Context 的
    Load / Save clip_index，所以用户只需要看这一个节点。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "公共提示词": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "dynamicPrompts": False,
                    "tooltip": "所有分段共用的一条提示词（接条件节点/CLIPTextEncode 的提示词）。"
                               "从第 2 段起节点会自动在其末尾追加无缝衔接说明。",
                }),
                "当前段": ("INT", {
                    "default": 1, "min": 1, "max": 99999,
                    "tooltip": "当前要生成的段号，从 1 开始。由节点面板的按钮自动推进，一般不需要手改。",
                }),
                "上下文帧数": ("INT", {
                    "default": 22, "min": 0, "max": 240,
                    "tooltip": "要与「🎬H3运动上下文」节点的 context_length 保持一致（默认 22）。"
                               "第 2 段起，本节点把 start_time 往前挪这么多帧、duration 相应加长，"
                               "让 H3 多渲染出衔接帧，再由「✂H3运动上下文裁剪」剪掉，本段交付时长才准确。"
                               "不做多段接力时设 0。",
                }),
                "帧率": ("FLOAT", {
                    "default": 24.0, "min": 1.0, "max": 240.0, "step": 0.001,
                    "tooltip": "把上下文帧数换算成秒用的帧率，必须与 H3 生成帧率一致（默认 24）。",
                }),
                "文件名前缀": ("STRING", {
                    "default": "h3_audio_chain",
                    "tooltip": "分段视频保存的基础名：接到视频保存节点（如 VHS_VideoCombine）的 filename_prefix，"
                               "实际前缀为 基础名/seg序号，分段都会存到 output/基础名/ 下；"
                               "画布上的合并节点也用这个前缀去该目录收集分段。",
                }),
            },
            "optional": {
                "识别文字并入提示词": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "把本段识别文字放到提示词最前面（视觉描述保留在后面）。",
                }),
                "自动连跑": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "服务端连跑：本段成功后由后端自动入队下一段，浏览器刷新、关闭都不影响。"
                               "合并与保存不在本节点做了——由画布上的合并/保存节点在收到「完成信号」时收尾。"
                               "只影响直接点队列「运行」的提交——面板按钮固定语义："
                               "「▶ 开始生成」「⏮ 从头开始」总是连跑，「⏭ 只跑这一段」总是单段。",
                }),
                "每轮段数": ("INT", {
                    "default": 0, "min": 0, "max": 9999,
                    "tooltip": "自动连跑每轮最多跑几段就停在停点（0 = 不限制，一路跑到最后并收尾）。"
                               "从「当前段」起算：设 4 表示本段起连跑 4 段后停下，方便中途检查或手动处理；"
                               "续跑时把「当前段」设成停点后一段再开始。真·全自动流程保持 0。",
                }),
                "分段JSON": ("STRING", {
                    "multiline": True,
                    "default": "",
                    "dynamicPrompts": False,
                    "tooltip": "分段计划：{"
                               "\"total\": 126.9, \"segments\": [{\"start_time\": 0.0, \"duration\": 10.1, "
                               "\"text\": \"…\"}, …]}，segments 也接受裸数组，每项的文字可用 text 或 prompt。"
                               "分段数、总时长、全文与本段音频起止都由它算出。"
                               "可直接粘贴，也可右键「转换为输入」接上游音频分段节点的「分段JSON」。",
                }),
            },
        }

    RETURN_TYPES = ("FLOAT", "FLOAT", "STRING", "FLOAT", "STRING", "STRING", "INT", "STRING", "BOOLEAN")
    RETURN_NAMES = ("start_time", "duration", "prompt", "本段时长", "文件名前缀", "文字", "总段数",
                    "分段JSON", "完成信号")
    FUNCTION = "resolve"
    CATEGORY = "TT_MiniMaxH3/生成串联"
    DESCRIPTION = ("音频分段串联控制台：分段完全由「分段JSON」决定——粘贴 JSON 或把它转换成输入接"
                   "上游音频分段节点皆可，本节点不依赖具体分段来源。分段数、总时长、全文识别文字"
                   "都由这份 JSON 算出，不再单独暴露输入。"
                   "输出 start_time / duration 给 VHS_LoadAudioUpload，公共提示词与本段识别文字并入 prompt。"
                   "从第 2 段起自动追加无缝衔接说明，start_time 会前移「上下文帧数/帧率」秒、"
                   "duration 相应加长，使被 ✂H3运动上下文裁剪 剪掉的衔接帧得到补偿。"
                   "开启「自动连跑」后由服务端接力：本段成功后自动入队下一段，浏览器刷新、关闭都不影响。"
                   "「完成信号」（只在最后一段为真）供画布上的合并/保存节点收尾。"
                   "「分段JSON」输出导出整份分段表（每段 start_time/duration/识别文字，裸数组），"
                   "可直接接到展示节点预览。"
                   "「每轮段数」大于 0 时，连跑从「当前段」起跑满这么多段就停在停点；设 0 为不限、一路跑到最后。")

    def resolve(self, 公共提示词, 当前段=1, 上下文帧数=22, 帧率=24.0,
                文件名前缀="h3_audio_chain", 识别文字并入提示词=True, 自动连跑=True,
                每轮段数=0, 分段JSON=""):
        plan = _parse_plan(分段JSON)
        segments = plan["segments"]
        if not segments:
            raise ValueError(
                "音频分段串联：「分段JSON」为空或没有有效分段。请在输入框里粘贴分段 JSON，"
                "或右键「分段JSON」→ 转换为输入，再接上游音频分段节点的「分段JSON」。")

        fps = float(帧率) if float(帧率) > 0 else 24.0
        count = len(segments)
        total = plan["total"]
        index = max(0, min(int(当前段) - 1, count - 1))
        entry = segments[index]
        seg_start = float(entry["start"])
        delivered = float(entry["duration"])
        asr_text = entry["text"]

        # 第 2 段起，上一段的尾部被钉在本段时间线的开头，并被 ✂Trim 剪掉，所以
        # 音频要从 seg_start 往前多截「上下文帧数/帧率」秒、duration 一并加长：
        # H3 多渲染出来的那几帧正好是衔接用的上下文帧，剪掉后本段内容才是 delivered 秒。
        overlap = 0.0
        if index >= 1 and int(上下文帧数) > 0:
            overlap = min(int(上下文帧数) / fps, seg_start)
        start_time = seg_start - overlap
        duration = delivered + overlap

        text = 公共提示词 or ""
        if asr_text and 识别文字并入提示词:
            text = (asr_text + "\n\n" + text).rstrip() if text.strip() else asr_text
        if index >= 1:
            note = _SEAMLESS_CONTINUE.format(n=index + 1, total=count)
            text = (text.rstrip() + "\n\n" + note) if text.strip() else note

        base = re.sub(r'[<>:"/\\|?*]', "_", str(文件名前缀 or "").strip()) or "h3_audio_chain"
        file_prefix = "%s/seg%03d" % (base, index + 1)
        full_text = plan["full_text"]

        # 分段计划与全文回传前端（含每段起止与文字），并登记到服务端供状态接口读取；
        # 只跑这一段（不连跑）时也登记，保证面板能显示真实分段与整段文字。
        ui_segments = [{"start": round(s["start"], 3), "duration": round(s["duration"], 3),
                        "text": s["text"]} for s in segments]
        # 整份分段计划一览（每段 start_time/duration/识别文字，裸数组），可直接接到
        # 「展示任意」等文本节点预览。
        segments_json = json.dumps(
            [{"start_time": s["start"], "duration": s["duration"], "text": s["text"]}
             for s in ui_segments], ensure_ascii=False)
        auto_chain.publish(base, ui_segments, full_text)

        if 自动连跑:
            auto_chain.schedule(index + 1, count, base, int(每轮段数 or 0))

        return {
            "ui": {"tt_audio_chain": {
                "mode": "plan", "count": count, "index": index,
                "total": round(total, 3), "full_text": full_text,
                "segments": ui_segments}},
            "result": (round(start_time, 4), round(duration, 4), text,
                       round(delivered, 4), file_prefix, asr_text, count,
                       segments_json, index + 1 >= count),
        }
