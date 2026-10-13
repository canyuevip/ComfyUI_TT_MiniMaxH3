"""曝光锁定合并：把分段视频拼成一条时，顺手把逐段累积的曝光/色调漂移拉回来。

只修新段开头、不整段调色：

  接缝修复   只修新段开头十几帧的低频亮度/色调，向上一段尾帧对齐；同一镜头内
             若首帧有跳变，用上一段的运动外插做一帧过渡；硬切镜头不动（避免变成叠化）。
  曝光锁定   以第一段的中位亮度为基准，对后面每段做有界、平滑的增益，压掉跨段
             缓慢变亮/变暗；段边界前几帧再逐帧钉一次，消掉 H3 常见的开头闪烁。

和「🎬视频合并（分段拼接）」的区别：那个是 ffmpeg 直接拼、无损秒级完成；本节点
要逐帧解码再编码，慢、会重编码，但能把长链的曝光漂移修掉。用不用取决于工作流。

分段视频按段号顺序取自 output/<文件名前缀>/ 下的 segNNN（与音频分段串联共用前缀）。
"""

import io
import os
import re
import shutil
import subprocess
import tempfile

import torch
import torch.nn.functional as F

try:
    import folder_paths
except ImportError:
    folder_paths = None

from .tt_ffmpeg_utils import collect_segment_files, find_ffmpeg, prefix_to_segment_dir

# ---- 合成器常数（数值原样保留） ----
EXPOSURE_LOCK_WINDOW = 9
EXPOSURE_LOCK_GAIN_WINDOW = 3
EXPOSURE_LOCK_MIN_GAIN = 0.75
EXPOSURE_LOCK_MAX_GAIN = 1.30
EXPOSURE_BOUNDARY_FRAMES = 10
EXPOSURE_BOUNDARY_MIN_GAIN = 0.70
EXPOSURE_BOUNDARY_MAX_GAIN = 1.25

SEAM_GRADE_FRAMES = 12
SEAM_GRADE_WEIGHT = 0.70
SEAM_GRADE_BLUR = 64
SEAM_ADD_LUMA_FRAMES = 12
SEAM_ADD_LUMA_MAX = 0.10
SEGMENT_LUMA_MAX = 0.04
SEGMENT_LUMA_EPSILON = 0.002
SEAM_LUMA_EPSILON = 0.008

SEAM_BRIDGE_RATIO = 1.75
SEAM_BRIDGE_MIN_STEP = 0.025
SEAM_BRIDGE_MAX_STEP = 0.090
SEAM_BRIDGE_CUT_DIFF = 0.045
SEAM_BRIDGE_MIN_WEIGHT = 0.10
SEAM_BRIDGE_MAX_WEIGHT = 0.45

_ENCODE_CRF = "19"
_ENCODE_PRESET = "medium"


def _smooth_signal(values, window):
    """移动平均；用来平滑亮度基线与增益。"""
    if not values:
        return []
    width = max(1, int(window))
    if width % 2 == 0:
        width += 1
    if width <= 1 or len(values) == 1:
        return [float(v) for v in values]
    radius = width // 2
    out = []
    for index in range(len(values)):
        total = 0.0
        for offset in range(-radius, radius + 1):
            source = min(max(index + offset, 0), len(values) - 1)
            total += float(values[source])
        out.append(total / float(width))
    return out


def _frame_luma(frame):
    coeffs = frame.new_tensor([0.299, 0.587, 0.114])
    return float((frame.float() * coeffs).sum(dim=-1).mean().item())


def _robust_luma(frames):
    """每帧亮度的中位数，忽略短闪与局部动作。"""
    if not isinstance(frames, torch.Tensor) or frames.ndim != 4 or frames.numel() == 0:
        return 0.0
    step = max(1, int(frames.shape[0]) // 24)
    sample = frames[::step].detach().float()
    coeffs = sample.new_tensor([0.299, 0.587, 0.114])
    per_frame = (sample * coeffs).sum(dim=-1).mean(dim=(1, 2))
    return float(per_frame.median().item())


def _fit_frame(frame, width, height):
    """把一张 HWC 帧缩放到目标尺寸（不依赖 H3 主流程的 helper）。"""
    if frame.dim() == 4:
        frame = frame[0]
    if tuple(frame.shape[:2]) == (int(height), int(width)):
        return frame
    resized = F.interpolate(
        frame.permute(2, 0, 1).unsqueeze(0).float(),
        size=(int(height), int(width)), mode="bilinear", align_corners=False,
    )[0]
    return resized.permute(1, 2, 0).to(dtype=frame.dtype)


def _box_blur(frames, kernel):
    """低频盒式模糊，用于只对「大范围明暗」对齐、不碰细节边缘。"""
    k = int(kernel)
    if k < 3:
        return frames.detach().float()
    if k % 2 == 0:
        k += 1
    source = frames.detach().float()
    if source.dim() == 3:
        source = source.unsqueeze(0)
    padded = F.pad(source.permute(0, 3, 1, 2), (k // 2, k // 2, k // 2, k // 2),
                   mode="replicate")
    blurred = F.avg_pool2d(padded, kernel_size=k, stride=1)
    return blurred.permute(0, 2, 3, 1)


def _match_segment_luma(frames, target_luma):
    """对整段加一个有界直流偏移，把整段基线亮度向目标靠。"""
    if target_luma is None or not isinstance(frames, torch.Tensor) or frames.numel() == 0:
        return frames, 0.0
    current = _robust_luma(frames)
    offset = max(-SEGMENT_LUMA_MAX, min(SEGMENT_LUMA_MAX, float(target_luma) - current))
    if abs(offset) < SEGMENT_LUMA_EPSILON:
        return frames, 0.0
    frames.add_(offset).clamp_(0.0, 1.0)
    return frames, float(offset)


def _match_opening_grade(frames, previous_tail):
    """只把新段开头的低频明暗/色调向上一段尾帧对齐，不动其余帧。"""
    if (not isinstance(frames, torch.Tensor) or not isinstance(previous_tail, torch.Tensor)
            or frames.numel() == 0 or previous_tail.numel() == 0):
        return frames
    count = min(int(SEAM_GRADE_FRAMES), int(frames.shape[0]))
    if count <= 0:
        return frames
    weights = [SEAM_GRADE_WEIGHT * (1.0 - float(i) / float(count)) for i in range(count)]
    if not weights or weights[-1] <= 0.0:
        return frames
    guide = _fit_frame(previous_tail[-1], int(frames.shape[2]), int(frames.shape[1]))
    try:
        source = frames[:count].float()
        source_blur = _box_blur(source, SEAM_GRADE_BLUR)
        guide_blur = _box_blur(guide.unsqueeze(0), SEAM_GRADE_BLUR)[0]
        weight = frames.new_tensor(weights).view(-1, 1, 1, 1)
        frames[:count] = (source + weight * (guide_blur.unsqueeze(0) - source_blur)
                          ).clamp_(0.0, 1.0).to(dtype=frames.dtype)
    except Exception as exc:
        print(f"[TTExposureMerge] 接缝低频匹配已跳过：{exc}")
    return frames


def _additive_opening_luma(frames, previous_tail):
    """把新段开头从上一段的亮度平缓过渡到自身水平（加性，保住局部对比）。"""
    if (not isinstance(frames, torch.Tensor) or not isinstance(previous_tail, torch.Tensor)
            or int(frames.shape[0]) < 3 or previous_tail.numel() == 0):
        return frames
    count = min(SEAM_ADD_LUMA_FRAMES, int(frames.shape[0]) - 1)
    if count < 2:
        return frames
    guide = _fit_frame(previous_tail[-1], int(frames.shape[2]), int(frames.shape[1]))
    y_start = _frame_luma(guide)
    y0 = _frame_luma(frames[0])
    y1 = _frame_luma(frames[min(1, count)])
    y2 = _frame_luma(frames[min(2, count)])
    y_end = _frame_luma(frames[count])
    if (abs(y0 - y_start) < SEAM_LUMA_EPSILON
            and abs(y1 - y0) + abs(y2 - y1) < SEAM_LUMA_EPSILON
            and abs(y_end - y_start) < SEAM_LUMA_EPSILON):
        return frames
    for index in range(count):
        t = float(index) / float(count)
        target = y_start * (1.0 - t) + y_end * t
        delta = max(-SEAM_ADD_LUMA_MAX,
                    min(SEAM_ADD_LUMA_MAX, target - _frame_luma(frames[index])))
        if abs(delta) >= 1e-5:
            frames[index] = (frames[index].float() + delta).clamp_(0.0, 1.0).to(dtype=frames.dtype)
    return frames


def _frame_mae(left, right):
    if left is None or right is None:
        return 0.0
    if left.dim() == 4:
        left = left[0]
    if right.dim() == 4:
        right = right[0]
    if tuple(left.shape) != tuple(right.shape):
        right = _fit_frame(right, int(left.shape[1]), int(left.shape[0]))
    return float((left.detach().float() - right.detach().float()).abs().mean().item())


def _motion_bridge(frames, previous_tail):
    """同一镜头内首帧若跳变，用上一段的运动外插做一帧过渡；硬切不动。"""
    if (not isinstance(frames, torch.Tensor) or not isinstance(previous_tail, torch.Tensor)
            or int(frames.shape[0]) < 2 or int(previous_tail.shape[0]) < 2):
        return frames, None
    w, h = int(frames.shape[2]), int(frames.shape[1])
    prev_prev = _fit_frame(previous_tail[-2], w, h)
    prev_last = _fit_frame(previous_tail[-1], w, h)
    body0 = _fit_frame(frames[0], w, h)
    body1 = _fit_frame(frames[1], w, h)

    previous_step = _frame_mae(prev_prev, prev_last)
    seam_step = _frame_mae(prev_last, body0)
    following_step = _frame_mae(body0, body1)
    regular_step = max(previous_step, following_step, 1e-6)
    jump_ratio = seam_step / regular_step
    if (seam_step < SEAM_BRIDGE_MIN_STEP or seam_step > SEAM_BRIDGE_MAX_STEP
            or jump_ratio < SEAM_BRIDGE_RATIO):
        return frames, None

    # 硬切镜头的低频差很大，掺一帧会变成叠化，所以放过不修。
    lowfreq_diff = _frame_mae(
        _box_blur(prev_last.unsqueeze(0), SEAM_GRADE_BLUR)[0],
        _box_blur(body0.unsqueeze(0), SEAM_GRADE_BLUR)[0])
    if lowfreq_diff > SEAM_BRIDGE_CUT_DIFF:
        return frames, None

    step_severity = ((seam_step - SEAM_BRIDGE_MIN_STEP)
                     / max(1e-6, SEAM_BRIDGE_MAX_STEP - SEAM_BRIDGE_MIN_STEP))
    ratio_severity = ((jump_ratio - SEAM_BRIDGE_RATIO) / max(1e-6, 5.0 - SEAM_BRIDGE_RATIO))
    severity = max(0.0, min(1.0, max(step_severity, ratio_severity)))
    weight = SEAM_BRIDGE_MIN_WEIGHT + (SEAM_BRIDGE_MAX_WEIGHT - SEAM_BRIDGE_MIN_WEIGHT) * severity

    predicted = (prev_last.float() * 2.0 - prev_prev.float()).clamp_(0.0, 1.0)
    bridge_frame = predicted * 0.65 + prev_last.float() * 0.35
    frames[0] = (body0.float() * (1.0 - weight) + bridge_frame * weight
                 ).clamp_(0.0, 1.0).to(dtype=frames.dtype)
    return frames, float(weight)


class _SeamMatcher:
    """接缝修复：逐段推进，记住上一段的尾帧与基线亮度。"""

    def __init__(self, enabled=True):
        self.enabled = bool(enabled)
        self.previous_tail = None
        self.previous_segment_luma = None

    def apply(self, frames, index):
        if not self.enabled or not isinstance(frames, torch.Tensor) or frames.numel() == 0:
            return frames
        if self.previous_tail is not None:
            frames, _bridge = _motion_bridge(frames, self.previous_tail)
            frames = _match_opening_grade(frames, self.previous_tail)
            frames, _offset = _match_segment_luma(frames, self.previous_segment_luma)
            frames = _additive_opening_luma(frames, self.previous_tail)
        # clone(), not contiguous(): a dim-0 slice is already contiguous, so
        # contiguous() hands back a VIEW and the caller's later in-place
        # edits would leak into this tail.
        self.previous_tail = frames[-min(3, int(frames.shape[0])):].detach().clone()
        self.previous_segment_luma = _robust_luma(frames)
        return frames


class _ExposureLock:
    """曝光锁定：以第一段中位亮度为基准，对后面每段做有界平滑增益。"""

    def __init__(self, enabled=True):
        self.enabled = bool(enabled)
        self.target = None
        self.history = []
        self.previous_output_luma = None

    def _frame_luma(self, frames):
        if not isinstance(frames, torch.Tensor) or frames.ndim != 4 or frames.shape[0] < 1:
            return []
        working = frames.detach()
        if working.dtype != torch.float32:
            working = working.float()
        if int(working.shape[-1]) < 3:
            return []
        if int(working.shape[1]) > 180 or int(working.shape[2]) > 320:
            sample = F.interpolate(working[..., :3].permute(0, 3, 1, 2), size=(180, 320), mode="area")
        else:
            sample = working[..., :3].permute(0, 3, 1, 2)
        luma = (sample[:, 0] * 0.2126 + sample[:, 1] * 0.7152
                + sample[:, 2] * 0.0722).flatten(1).median(dim=1).values
        return [float(v) for v in luma.tolist()]

    def apply(self, frames, index):
        if not self.enabled or not isinstance(frames, torch.Tensor) or frames.numel() == 0:
            return frames
        luma = self._frame_luma(frames)
        if not luma:
            return frames
        if self.target is None:
            ordered = sorted(luma)
            self.target = float(ordered[len(ordered) // 2])
            self.history = luma[-12:]

        source = self.history + luma
        baseline = _smooth_signal(source, EXPOSURE_LOCK_WINDOW)[len(self.history):]
        gains = [min(EXPOSURE_LOCK_MAX_GAIN,
                     max(EXPOSURE_LOCK_MIN_GAIN, float(self.target) / max(v, 1e-4)))
                 for v in baseline]
        gains = _smooth_signal(gains, EXPOSURE_LOCK_GAIN_WINDOW)
        if self.previous_output_luma is not None:
            boundary_count = min(EXPOSURE_BOUNDARY_FRAMES, len(gains))
            for i in range(boundary_count):
                projected = float(luma[i]) * float(gains[i])
                factor = min(EXPOSURE_BOUNDARY_MAX_GAIN,
                             max(EXPOSURE_BOUNDARY_MIN_GAIN,
                                 float(self.previous_output_luma) / max(projected, 1e-4)))
                alpha = 1.0 - i / float(max(1, boundary_count))
                gains[i] *= 1.0 + (factor - 1.0) * alpha
        if len(gains) != int(frames.shape[0]):
            return frames
        for i, gain in enumerate(gains):
            frames[i].mul_(float(gain)).clamp_(0.0, 1.0)
        self.history = (self.history + luma)[-12:]
        self.previous_output_luma = float(luma[-1]) * float(gains[-1])
        return frames


def _probe_video(path):
    """(width, height, fps)；用 PyAV 读，缺 av 时报清楚。"""
    import av
    with av.open(path) as container:
        stream = container.streams.video[0]
        width = int(stream.codec_context.width)
        height = int(stream.codec_context.height)
        fps = float(stream.average_rate) if stream.average_rate else 24.0
    return width, height, fps


def _decode_segment(path):
    """把一个分段解成 float32 BHWC（0..1）张量。"""
    import av
    import numpy as np
    frames = []
    with av.open(path) as container:
        for frame in container.decode(video=0):
            frames.append(frame.to_ndarray(format="rgb24"))
    if not frames:
        raise RuntimeError("分段视频没有可用画面：%s" % path)
    return torch.from_numpy(np.stack(frames)).float().div_(255.0)


def merge_segment_dir_graded(dir_value, out_name="merged", exposure_lock=True,
                             seam_repair=True, expect=0, ffmpeg_override=""):
    """逐段解码 -> 接缝修复/曝光锁定 -> 重编码拼成一条。返回 dict。

    成功: {"ok": True, "filename", "subfolder", "path", "count"}
    失败: {"ok": False, "error"}
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
    if not ffmpeg:
        return {"ok": False, "error": "未找到 ffmpeg"}

    width, height, fps = _probe_video(files[0])
    workdir = tempfile.mkdtemp(prefix="tt_exposure_")
    concat_list = os.path.join(workdir, "concat.txt")
    audio_path = os.path.join(workdir, "audio.m4a")
    try:
        with io.open(concat_list, "w", encoding="utf-8") as fh:
            for path in files:
                fh.write("file '%s'\n" % os.path.abspath(path).replace("\\", "/").replace("'", "'\\''"))
        # 音轨：分段编码一致，concat 后直接 copy，不重编码。
        audio_ok = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
             "-safe", "0", "-i", concat_list, "-vn", "-c:a", "copy", audio_path],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
        ).returncode == 0 and os.path.isfile(audio_path)

        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", "%dx%d" % (width, height), "-r", "%.6f" % fps, "-i", "-"]
        if audio_ok:
            cmd += ["-i", audio_path, "-map", "0:v:0", "-map", "1:a:0", "-c:a", "copy", "-shortest"]
        cmd += ["-c:v", "libx264", "-preset", _ENCODE_PRESET, "-crf", _ENCODE_CRF,
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path]
        encoder = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        matcher = _SeamMatcher(seam_repair)
        lock = _ExposureLock(exposure_lock)
        try:
            for index, path in enumerate(files, 1):
                frames = _decode_segment(path)
                frames = matcher.apply(frames, index)
                frames = lock.apply(frames, index)
                # Non-in-place: the matcher holds a tail derived from this
                # tensor, so it must not be scaled to 0..255 behind its back.
                payload = (frames.clamp(0.0, 1.0) * 255.0).round().to(torch.uint8)
                encoder.stdin.write(payload.numpy().tobytes())
                print("[TTExposureMerge] 第 %d/%d 段已处理" % (index, len(files)))
        finally:
            try:
                encoder.stdin.close()
            except Exception:
                pass
        stderr = encoder.stderr.read().decode("utf-8", "ignore")
        code = encoder.wait()
        if code != 0 or not os.path.isfile(out_path):
            return {"ok": False, "error": (stderr or "").strip()[-800:] or "ffmpeg 编码失败"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return {"ok": True, "filename": name, "subfolder": subfolder, "path": out_path, "count": len(files)}


class TTExposureMerge:
    """把分段视频拼成一条，同时做接缝修复与跨段曝光锁定（需要时再挂这个节点）。"""

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
                    "default": "merged_graded",
                    "tooltip": "合并文件名（无需扩展名，自动补 .mp4），输出到分段所在的同一目录。",
                }),
                "曝光锁定": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "以第一段的中位亮度为基准，对后面每段做有界平滑增益，压掉长链逐段累积的"
                               "变亮/变暗；段边界前几帧再逐帧钉一次，消掉开头闪烁。",
                }),
                "接缝修复": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "只修新段开头十几帧的低频亮度/色调向上一段尾帧对齐；同一镜头内首帧跳变"
                               "用上一段运动外插补一帧；硬切镜头不动。",
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
                               "本节点与后面的保存节点都会被跳过；最后一段为真时才逐段解码处理并合并。"
                               "不接线或保持默认 True 时每次都直接合并（旧工作流行为不变）。",
                }),
                "触发对齐": ("*", {
                    "forceInput": True,
                    "tooltip": "只为连线顺序：接视频保存节点（如 VideoCombine）的输出，保证本节点在"
                               "本段 MP4 落盘后才开始处理；运行时不读取这个值。",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "VIDEO")
    RETURN_NAMES = ("合并路径", "合并视频")
    FUNCTION = "merge"
    CATEGORY = "TT_MiniMaxH3/视频处理"
    OUTPUT_NODE = True
    DESCRIPTION = ("曝光锁定合并：逐段解码后先做接缝修复（只修新段开头，向上一段尾帧对齐低频明暗）"
                   "与跨段曝光锁定（以第一段亮度为基准做有界增益），再重编码拼成一条 MP4。"
                   "用来压掉长链逐段累积的曝光/色调漂移。"
                   "会重编码、比「🎬视频合并（分段拼接）」慢，按需使用。"
                   "「全部完成」接串连控制台的「完成信号」后，连跑中途会跳过本节点、最后一段才合并；"
                   "「合并视频」输出可直接接 SaveVideo 等保存节点给平台 API 返回。")

    def merge(self, 文件名前缀, 合并输出名="merged_graded", 曝光锁定=True, 接缝修复=True,
              总段数=0, ffmpeg路径="", 全部完成=True, 触发对齐=None):
        from comfy_execution.graph import ExecutionBlocker
        from comfy_api.latest import InputImpl
        if not 全部完成:
            # 连跑还没到最后一段：拦住本节点和后面的保存节点，等「完成信号」打开再逐段处理。
            return {"result": (ExecutionBlocker(None), ExecutionBlocker(None))}
        res = merge_segment_dir_graded(
            文件名前缀, 合并输出名, bool(曝光锁定), bool(接缝修复), int(总段数 or 0), ffmpeg路径)
        if not res.get("ok"):
            text = "❌ " + res.get("error", "合并失败")
            print("[TTExposureMerge] " + text)
            return {"ui": {"text": [text]}, "result": ("", ExecutionBlocker(None))}
        text = "✅ 已处理并合并 %d 段 -> %s" % (res["count"], res["path"])
        print("[TTExposureMerge] " + text)
        return {"ui": {"text": [text]},
                "result": (res["path"], InputImpl.VideoFromFile(res["path"]))}
