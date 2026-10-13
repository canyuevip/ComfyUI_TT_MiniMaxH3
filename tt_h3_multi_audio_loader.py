import os

import torch
import folder_paths

MAX_AUDIOS = 3
SILENCE_SR = 44100
SILENCE_CHANNELS = 2


def f32_pcm(wav: torch.Tensor) -> torch.Tensor:
    """转为 float32 PCM，便于统一处理。"""
    if wav.dtype.is_floating_point:
        return wav
    elif wav.dtype == torch.int16:
        return wav.float() / (2 ** 15)
    elif wav.dtype == torch.int32:
        return wav.float() / (2 ** 31)
    raise ValueError(f"不支持的音频 dtype: {wav.dtype}")


def load_audio_file(filepath: str):
    """用 PyAV 解码音频，返回 (waveform[channels,time], sample_rate)。"""
    import av

    with av.open(filepath) as af:
        if not af.streams.audio:
            raise ValueError("文件中没有音频流")
        stream = af.streams.audio[0]
        sr = stream.codec_context.sample_rate
        n_channels = stream.channels

        frames = []
        for frame in af.decode(streams=stream.index):
            buf = torch.from_numpy(frame.to_ndarray())
            if buf.shape[0] != n_channels:
                buf = buf.view(-1, n_channels).t()
            frames.append(buf)

        if not frames:
            raise ValueError("未解码到任何音频帧")

        wav = torch.cat(frames, dim=1)
        wav = f32_pcm(wav)
        return wav, sr


def _silence_audio():
    """1 秒静音作为缺失/失败时的兜底输出。"""
    return {"waveform": torch.zeros((1, SILENCE_CHANNELS, SILENCE_SR)), "sample_rate": SILENCE_SR}


def _trim_audio(filepath: str, start_time: float, end_time: float):
    """加载并按秒修剪音频，返回 ComfyUI 标准 AUDIO dict。"""
    waveform, sr = load_audio_file(filepath)
    start_frame = int(max(0.0, start_time) * sr)
    if end_time > 0:
        end_frame = int(end_time * sr)
        end_frame = min(end_frame, waveform.shape[1])
    else:
        end_frame = waveform.shape[1]
    start_frame = min(start_frame, end_frame)
    trimmed = waveform[:, start_frame:end_frame]
    if trimmed.shape[1] == 0:
        trimmed = torch.zeros((waveform.shape[0], 1))
    return {"waveform": trimmed.unsqueeze(0), "sample_rate": sr}


# 自定义 API 路由：用于在浏览器中播放“绝对路径”音频（/view 只服务 input/output/temp）。
# 支持 Range 请求，保证播放器可拖动进度条。
try:
    from aiohttp import web
    from server import PromptServer

    _H3_AUDIO_ROUTE = "/tt/h3/audio"

    _AUDIO_MIME = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".opus": "audio/opus",
        ".webm": "audio/webm",
        ".wma": "audio/x-ms-wma",
    }

    def _is_route_registered(server, method, path):
        try:
            for d in server.routes._items:
                if getattr(d, "method", None) == method and getattr(d, "path", None) == path:
                    return True
        except Exception:
            pass
        return False

    if not _is_route_registered(PromptServer.instance, "GET", _H3_AUDIO_ROUTE):
        @PromptServer.instance.routes.get(_H3_AUDIO_ROUTE)
        async def _serve_h3_audio(request):
            path = request.query.get("path", "")
            if not path or not os.path.isfile(path):
                return web.Response(status=404)
            try:
                size = os.path.getsize(path)
                content_type = _AUDIO_MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
                headers = {"Accept-Ranges": "bytes"}

                rng = request.headers.get("Range")
                if rng:
                    start = end = None
                    try:
                        if rng.startswith("bytes="):
                            rng = rng[len("bytes="):]
                            if "-" in rng:
                                a, b = rng.split("-", 1)
                                start = int(a) if a else None
                                end = int(b) if b else None
                    except ValueError:
                        start = end = None
                    if start is None:
                        start = 0
                    if end is None or end >= size:
                        end = size - 1
                    if start > end or start >= size:
                        return web.Response(status=416)
                    length = end - start + 1
                    with open(path, "rb") as f:
                        f.seek(start)
                        body = f.read(length)
                    headers.update({
                        "Content-Range": f"bytes {start}-{end}/{size}",
                        "Content-Length": str(length),
                    })
                    return web.Response(body=body, status=206, content_type=content_type, headers=headers)

                with open(path, "rb") as f:
                    body = f.read()
                headers["Content-Length"] = str(size)
                return web.Response(body=body, content_type=content_type, headers=headers)
            except Exception:
                return web.Response(status=500)
except Exception:
    pass


class TTH3MultiAudioLoader:
    """MiniMax H3 多音频加载器：固定 3 路 AUDIO 输出。
    每路音频可独立设置 start/end 进行修剪（默认整段），并带播放预览。
    支持在输入框粘贴音频绝对路径（每行一个，最多 3 个），或通过 Upload 上传（最多 3 个）。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "audio_paths": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": True,
                        "placeholder": "粘贴音频绝对路径，每行一个，最多 3 个",
                    },
                ),
                "start_time_1": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 100000.0, "step": 0.01}),
                "end_time_1": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 100000.0, "step": 0.01}),
                "start_time_2": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 100000.0, "step": 0.01}),
                "end_time_2": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 100000.0, "step": 0.01}),
                "start_time_3": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 100000.0, "step": 0.01}),
                "end_time_3": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 100000.0, "step": 0.01}),
            },
        }

    RETURN_TYPES = ("AUDIO",) * MAX_AUDIOS
    RETURN_NAMES = tuple(f"audio_{i + 1}" for i in range(MAX_AUDIOS))
    FUNCTION = "load_audios"
    CATEGORY = "TT_MiniMaxH3/音频处理"

    def load_audios(self, audio_paths, start_time_1, end_time_1, start_time_2, end_time_2, start_time_3, end_time_3):
        paths = [p.strip() for p in (audio_paths or "").splitlines() if p.strip()][:MAX_AUDIOS]
        starts = [start_time_1, start_time_2, start_time_3]
        ends = [end_time_1, end_time_2, end_time_3]

        results = []
        for i in range(MAX_AUDIOS):
            if i < len(paths):
                full = paths[i]
                if not os.path.exists(full):
                    full = os.path.join(folder_paths.get_input_directory(), paths[i])
                if os.path.exists(full):
                    try:
                        results.append(_trim_audio(full, starts[i], ends[i]))
                        continue
                    except Exception as e:
                        print(f"[TTH3MultiAudioLoader] 音频加载失败 {paths[i]}: {e}")
            results.append(_silence_audio())

        return tuple(results)
