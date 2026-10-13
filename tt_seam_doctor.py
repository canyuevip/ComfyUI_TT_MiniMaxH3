"""接缝诊断与桥帧门控：给 H3 串联长链做「接缝体检」，并在续接前挡住坏尾。

只用 torch，不引入新依赖；只测不治、只选不修。

  TTBridgeFrameGate  桥帧门控：给上一段尾帧打分（Laplacian 清晰度 + 曝光），
                        尾帧烂就回退到 17/34 帧再切，输出选好的桥帧窗口/偏移帧/
                        通过与否。offset 用于人工或后续接线把上下文窗口前移。
  TTSeamDoctor       接缝医生：对相邻两段做五维 z-score（运动量/位移/颜色/
                        清晰度/音频）体检，只输出报告，不自动修。排查「第 4 段
                        机位漂移」这类问题不必只靠肉眼。
"""

import json

import torch
import torch.nn.functional as F

_LAPLACIAN = torch.tensor(
    [[0.0, 1.0, 0.0], [1.0, -4.0, 1.0], [0.0, 1.0, 0.0]]).view(1, 1, 3, 3)

# 打分只在乎相对高低，统一缩到同一尺度即可，也顺便压掉 conv/fft 的开销
_METRIC_EDGE = 256
# 段太长时均匀抽样统计内部基线，避免整段逐帧卷积
_METRIC_MAX_FRAMES = 48
_BRIDGE_STEPS = (17, 34)


def _as_batch(images):
    if not isinstance(images, torch.Tensor) or images.ndim != 4:
        raise ValueError("需要 IMAGE 批次 [N,H,W,C]，收到 %r"
                         % (tuple(images.shape) if isinstance(images, torch.Tensor) else type(images)))
    if images.shape[0] < 1:
        raise ValueError("图像批次为空")
    return images


def _gray(images, edge=_METRIC_EDGE):
    """缩到统一尺度并转灰度，返回 [N,1,h,w] float32（0..1）。"""
    x = images[..., :3].float().movedim(-1, 1)
    if int(x.shape[-1]) > edge or int(x.shape[-2]) > edge:
        scale = float(edge) / float(max(int(x.shape[-1]), int(x.shape[-2])))
        x = F.interpolate(x, scale_factor=scale, mode="area")
    return (x[:, 0] * 0.299 + x[:, 1] * 0.587 + x[:, 2] * 0.114).unsqueeze(1)


def _sample(images, cap=_METRIC_MAX_FRAMES):
    count = int(images.shape[0])
    if count <= cap:
        return images
    step = count / float(cap)
    idx = [min(count - 1, int(round(i * step))) for i in range(cap)]
    return images[torch.tensor(sorted(set(idx)), dtype=torch.long)]


def _laplacian_var(gray):
    """灰度批 [N,1,h,w] -> 每帧 Laplacian 方差 [N]，越大越清晰。"""
    lap = F.conv2d(gray, _LAPLACIAN.to(device=gray.device, dtype=gray.dtype), padding=1)
    return lap.flatten(1).var(dim=1, unbiased=False)


def _mean_luma(images):
    return float(_gray(images[:1]).mean().item())


def _global_shift(a_gray, b_gray):
    """相位相关估全局平移，返回 (dy,dx) 像素。用来量化机位/位移跳变。"""
    fa = torch.fft.rfft2(a_gray)
    fb = torch.fft.rfft2(b_gray)
    cross = fa * fb.conj()
    cross = cross / cross.abs().clamp_min(1e-6)
    corr = torch.fft.irfft2(cross, s=a_gray.shape[-2:])
    h, w = int(a_gray.shape[-2]), int(a_gray.shape[-1])
    peak = int(corr.reshape(-1).argmax().item())
    dy, dx = divmod(peak, w)
    if dy > h // 2:
        dy -= h
    if dx > w // 2:
        dx -= w
    return dy, dx


def _hist_distance(a, b, bins=16):
    """两张帧的逐通道直方图 L1 距离，0..1。"""
    def hist(frame):
        pixels = frame[..., :3].reshape(-1, 3).clamp(0.0, 1.0)
        index = (pixels * (bins - 1)).round().long()
        out = torch.zeros(3, bins, device=frame.device)
        for channel in range(3):
            out[channel].scatter_add_(
                0, index[:, channel], torch.ones_like(index[:, channel], dtype=torch.float32))
        return out / out.sum(dim=1, keepdim=True).clamp_min(1e-6)
    return float((hist(a) - hist(b)).abs().sum().item() / 6.0)


def _rms(waveform):
    total = float(waveform.float().pow(2).mean().item())
    return total ** 0.5


def _z(value, samples, eps=1e-6):
    if not samples:
        return 0.0
    tensor = torch.tensor([float(v) for v in samples], dtype=torch.float32)
    if tensor.numel() < 2:
        return 0.0
    sd = float(tensor.std(unbiased=False).item())
    if sd < eps:
        return 0.0
    return float((float(value) - float(tensor.mean().item())) / sd)


def _consecutive_diffs(grays):
    return [float((grays[i + 1] - grays[i]).abs().mean().item())
            for i in range(int(grays.shape[0]) - 1)]


class TTBridgeFrameGate:
    """桥帧门控：续接前给上一段尾帧打分，尾帧烂就回退 17/34 帧再切。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "上一段画面": ("IMAGE", {
                    "tooltip": "上一段裁剪后的成片画面（IMAGE 批次）。用它给尾帧打分，"
                               "挑出最干净的一帧作为续接的桥帧。"}),
                "窗口帧数": ("INT", {
                    "default": 22, "min": 1, "max": 512,
                    "tooltip": "要交给下一段的桥帧窗口长度，通常与运动上下文的 context_length 一致。"}),
            },
            "optional": {
                "清晰度容差": ("FLOAT", {
                    "default": 1.0, "min": 0.0, "max": 6.0, "step": 0.1,
                    "tooltip": "尾帧 Laplacian 清晰度相对本段中位数的 z 分低于 -容差就判为「糊尾」，"
                               "触发回退。越大越宽松。"}),
                "曝光下限": ("FLOAT", {
                    "default": 0.03, "min": 0.0, "max": 1.0, "step": 0.01,
                    "tooltip": "尾帧平均亮度低于此值判为「黑帧」。只挡明显死黑，正常暗场不会误伤。"}),
                "曝光上限": ("FLOAT", {
                    "default": 0.97, "min": 0.0, "max": 1.0, "step": 0.01,
                    "tooltip": "尾帧平均亮度高于此值判为「死白」。只挡明显过曝。"}),
            },
        }

    RETURN_TYPES = ("IMAGE", "INT", "BOOLEAN", "FLOAT", "STRING")
    RETURN_NAMES = ("桥帧窗口", "偏移帧", "通过", "清晰度Z", "诊断")
    FUNCTION = "gate"
    CATEGORY = "TT_MiniMaxH3/视频处理"
    DESCRIPTION = ("桥帧门控：给上一段尾帧算 Laplacian 清晰度与曝光，"
                   "尾帧糊/死黑/死白就回退到尾部 17 或 34 帧再切，避免「从一张糊尾继续长」。"
                   "输出选好的桥帧窗口（可接运动上下文的 context_frames）与偏移帧，"
                   "offset 在 latent 路径下只能按 17 帧的整数倍前移，按需手工使用。")

    def gate(self, 上一段画面, 窗口帧数, 清晰度容差=1.0, 曝光下限=0.03, 曝光上限=0.97):
        frames = _as_batch(上一段画面)
        total = int(frames.shape[0])
        window = max(1, int(窗口帧数))
        if window > total:
            raise ValueError("窗口帧数 %d 超过上一段总帧数 %d" % (window, total))

        sharp = _laplacian_var(_gray(_sample(frames)))
        values = [float(v) for v in sharp.tolist()]
        order = sorted(values)
        median = order[len(order) // 2]
        spread = float(torch.tensor(values).std(unbiased=False).item())

        def score_frame(frame):
            var = float(_laplacian_var(_gray(frame.unsqueeze(0)))[0].item())
            z = 0.0 if spread < 1e-9 else (var - median) / spread
            luma = _mean_luma(frame.unsqueeze(0))
            ok = z >= -float(清晰度容差) and float(曝光下限) <= luma <= float(曝光上限)
            return z, luma, ok

        chosen, report = None, []
        for offset in (0,) + _BRIDGE_STEPS:
            if offset >= total:
                continue
            z, luma, ok = score_frame(frames[total - 1 - offset])
            report.append({"偏移帧": offset, "清晰度Z": round(z, 3),
                           "亮度": round(luma, 4), "可用": bool(ok)})
            if ok and chosen is None:
                chosen = (offset, z)
        # 没有一帧达标时，退回清晰度最高的候选，至少不选最糊的
        if chosen is None:
            best = max(report, key=lambda item: (item["清晰度Z"], -item["偏移帧"]))
            chosen = (best["偏移帧"], best["清晰度Z"])

        offset, z = chosen
        end = total - offset
        start = max(0, end - window)
        picked = frames[start:end]
        passed = offset == 0
        verdict = ("✅ 尾帧可用，按原样续接" if passed
                   else "⚠️ 尾帧不达标，已回退 %d 帧再切" % offset)
        text = verdict + "\n" + json.dumps(
            {"偏移帧": offset, "清晰度Z": round(z, 3), "候选": report},
            ensure_ascii=False, indent=2)
        print("[TTBridgeFrameGate] " + verdict)
        return (picked, int(offset), bool(passed), float(z), text)


class TTSeamDoctor:
    """接缝医生：相邻两段做五维 z-score 体检，只测不治。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "上一段画面": ("IMAGE", {"tooltip": "前一段的裁剪后画面（IMAGE 批次）。"}),
                "下一段画面": ("IMAGE", {"tooltip": "紧接其后的那一段画面（IMAGE 批次）。"}),
                "比较窗口": ("INT", {
                    "default": 12, "min": 1, "max": 128,
                    "tooltip": "接缝两侧各取多少帧算内部基线；太短会不稳，太长会把段内运动算进去。"}),
                "报警阈值": ("FLOAT", {
                    "default": 3.0, "min": 0.5, "max": 8.0, "step": 0.1,
                    "tooltip": "任一维 z 分绝对值超过它就算「接缝异常」。3 约等于 99.7% 置信带。"}),
            },
            "optional": {
                "上一段音频": ("AUDIO", {"tooltip": "可选。给响度对齐/音频跳变那一维用；不给则该维记 0。"}),
                "下一段音频": ("AUDIO", {"tooltip": "可选。与上一段音频一起提供才有音频体检。"}),
            },
        }

    RETURN_TYPES = ("BOOLEAN", "FLOAT", "FLOAT", "FLOAT", "FLOAT", "FLOAT", "FLOAT", "STRING")
    RETURN_NAMES = ("通过", "最大Z", "运动Z", "位移Z", "颜色Z", "清晰度Z", "音频Z", "诊断")
    FUNCTION = "inspect"
    CATEGORY = "TT_MiniMaxH3/视频处理"
    DESCRIPTION = ("接缝医生（五维 z-score）：对相邻两段在运动量、"
                   "全局位移、颜色直方图、清晰度、音频响度五个维度上，把接缝处的跳变量和"
                   "段内基线比出 z 分，输出诊断报告。只测量、不自动修，用来客观定位机位漂移、"
                   "画质骤降、响度跳变这类长链问题。")

    def inspect(self, 上一段画面, 下一段画面, 比较窗口, 报警阈值=3.0,
                上一段音频=None, 下一段音频=None):
        window = max(1, int(比较窗口))
        a_frames = _as_batch(上一段画面)
        b_frames = _as_batch(下一段画面)
        if int(a_frames.shape[0]) < 2 or int(b_frames.shape[0]) < 2:
            raise ValueError("两段各需至少 2 帧才能算基线")

        a_gray = _gray(_sample(a_frames))
        b_gray = _gray(_sample(b_frames))
        a_diffs = _consecutive_diffs(a_gray)
        b_diffs = _consecutive_diffs(b_gray)

        # 1) 运动量：接缝帧差 vs 两段内部帧差
        seam_flow = float((b_gray[0] - a_gray[-1]).abs().mean().item())
        motion_z = _z(seam_flow, a_diffs[-window:] + b_diffs[:window])

        # 2) 位移：接缝全局平移 vs 段内相邻帧平移
        seam_dy, seam_dx = _global_shift(a_gray[-1], b_gray[0])
        seam_shift = (seam_dy ** 2 + seam_dx ** 2) ** 0.5
        inner_shifts = []
        for pair in (list(zip(a_gray[-window - 1:-1], a_gray[-window:]))
                     + list(zip(b_gray[:-1], b_gray[1:window + 1]))):
            dy, dx = _global_shift(pair[0], pair[1])
            inner_shifts.append((dy ** 2 + dx ** 2) ** 0.5)
        drift_z = _z(seam_shift, inner_shifts)

        # 3) 颜色：接缝两侧均值帧的直方图距离 vs 段内相邻帧
        a_mean = a_frames[-(min(window, int(a_frames.shape[0]))):].mean(dim=0, keepdim=True)
        b_mean = b_frames[:min(window, int(b_frames.shape[0]))].mean(dim=0, keepdim=True)
        seam_color = _hist_distance(a_mean[0], b_mean[0])
        inner_color = []
        for side in (a_frames[-min(window + 1, int(a_frames.shape[0])):],
                     b_frames[:min(window + 1, int(b_frames.shape[0]))]):
            for i in range(int(side.shape[0]) - 1):
                inner_color.append(_hist_distance(side[i], side[i + 1]))
        color_z = _z(seam_color, inner_color)

        # 4) 清晰度：尾帧相对本段中位数的 z 分（负值 = 尾帧变糊）
        a_vars = [float(v) for v in _laplacian_var(a_gray).tolist()]
        b_vars = [float(v) for v in _laplacian_var(b_gray).tolist()]
        sharp_z = _z(float(b_vars[0]), a_vars + b_vars)

        # 5) 音频：接缝两侧响度差（dB）vs 段内相邻窗响度差
        audio_z = 0.0
        audio_db = None
        if 上一段音频 is not None and 下一段音频 is not None:
            a_rms = _rms(上一段音频["waveform"][..., -1600:])
            b_rms = _rms(下一段音频["waveform"][..., :1600])
            seam_db = abs(20.0 * torch.log10(torch.tensor(max(b_rms, 1e-6)) / max(a_rms, 1e-6)).item())
            audio_db = float(seam_db)
            base = []
            for waveform in (上一段音频["waveform"], 下一段音频["waveform"]):
                length = int(waveform.shape[-1])
                block = 1600
                levels = [_rms(waveform[..., i:i + block])
                          for i in range(0, max(1, length - block), block)]
                for i in range(len(levels) - 1):
                    base.append(abs(20.0 * torch.log10(
                        torch.tensor(max(levels[i + 1], 1e-6)) / max(levels[i], 1e-6)).item()))
            audio_z = _z(seam_db, base)

        axes = {"运动Z": motion_z, "位移Z": drift_z, "颜色Z": color_z,
                "清晰度Z": sharp_z, "音频Z": audio_z}
        peak = max(abs(v) for v in axes.values())
        passed = peak <= float(报警阈值)
        report = {
            "通过": bool(passed),
            "最大Z": round(peak, 3),
            "报警阈值": float(报警阈值),
            "各维Z": {k: round(v, 3) for k, v in axes.items()},
            "接缝": {"运动量": round(seam_flow, 5), "位移px": round(seam_shift, 3),
                     "位移方向": [seam_dy, seam_dx], "颜色距离": round(seam_color, 4),
                     "清晰度": round(float(b_vars[0]), 6),
                     "音频dB": None if audio_db is None else round(audio_db, 2)},
        }
        verdict = "✅ 接缝在阈值内" if passed else "⚠️ 接缝异常，最大 |Z|=%.2f" % peak
        text = verdict + "\n" + json.dumps(report, ensure_ascii=False, indent=2)
        print("[TTSeamDoctor] " + verdict)
        return (bool(passed), float(peak), float(motion_z), float(drift_z), float(color_z),
                float(sharp_z), float(audio_z), text)