# -*- coding: utf-8 -*-
"""ffmpeg 查找与分段视频收集/合并工具（供视频合并、曝光合并节点使用）。"""
import io
import os
import re
import shutil
import subprocess

_FFMPEG_CACHE = {"resolved": False, "path": None}
_VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm")


def find_ffmpeg():
    """按优先级查找 ffmpeg（插件 bin/ 内置副本可选，缺失自动回落）。

    环境变量 FFMPEG_PATH -> 插件目录 bin/ffmpeg.exe
    -> imageio_ffmpeg（随 requirements.txt 安装）-> 系统 PATH
    """
    if _FFMPEG_CACHE["resolved"]:
        return _FFMPEG_CACHE["path"]
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.environ.get("FFMPEG_PATH"),
        os.path.join(here, "bin", "ffmpeg.exe"),
        os.path.join(here, "bin", "ffmpeg"),
    ]
    try:
        import imageio_ffmpeg
        cands.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    cands.append(shutil.which("ffmpeg"))
    for c in cands:
        try:
            if c and os.path.isfile(str(c)):
                _FFMPEG_CACHE["resolved"] = True
                _FFMPEG_CACHE["path"] = str(c)
                print(f"[TT] ffmpeg: {_FFMPEG_CACHE['path']}")
                return _FFMPEG_CACHE["path"]
        except Exception:
            continue
    _FFMPEG_CACHE["resolved"] = True
    _FFMPEG_CACHE["path"] = None
    print("[TT] !! 未找到 ffmpeg，视频合并不可用（可设置环境变量 FFMPEG_PATH）")
    return None


def collect_segment_files(dir_path):
    """收集目录里的 segNNN 分段视频，按段号排序；同段号多个文件取文件名最大者（最近一次生成）。"""
    best = {}
    try:
        names = os.listdir(dir_path)
    except OSError:
        return []
    for name in names:
        if not name.lower().endswith(_VIDEO_EXT):
            continue
        m = re.match(r"seg(\d+)", name, re.IGNORECASE)
        if not m:
            continue
        num = int(m.group(1))
        if num not in best or name > best[num]:
            best[num] = name
    return [os.path.join(dir_path, best[k]) for k in sorted(best)]


def prefix_to_segment_dir(dir_value):
    """把前缀还原成分段所在目录。

    控制台的「文件名前缀」输出是逐段形态（基础名/segNNN，供视频保存节点命名
    每段文件）；合并节点要的是它所在目录，所以剥掉末尾的 segNNN 一段。
    普通的基础名或绝对路径原样返回。
    """
    return re.sub(r"[\\/]\s*seg\d+\s*$", "", str(dir_value or "").strip(), flags=re.IGNORECASE)


def merge_videos(files, out_path, reencode=False, ffmpeg=None):
    """用 ffmpeg concat 拼接分段视频，默认 -c copy（不重编码）。返回 (成功, 错误信息)。"""
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return False, "未找到 ffmpeg"
    list_path = out_path + ".concat.txt"
    with io.open(list_path, "w", encoding="utf-8") as f:
        for p in files:
            f.write("file '%s'\n" % os.path.abspath(p).replace("\\", "/").replace("'", "'\\''"))
    codec = (["-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "192k"] if reencode else ["-c", "copy"])
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
           "-i", list_path] + codec + ["-movflags", "+faststart", out_path]
    print(f"[TT] ffmpeg 合并 {len(files)} 个分段 -> {out_path}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    except Exception as e:
        return False, str(e)
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass
    if proc.returncode != 0:
        return False, (proc.stderr or "").strip()[-800:]
    return True, ""
