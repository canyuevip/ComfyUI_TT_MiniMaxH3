import io
import os

import numpy as np
import torch
from PIL import Image, ImageOps
import folder_paths

MAX_IMAGES = 9

# 自定义 API 路由：用于在节点画布中预览“绝对路径”图片。
# ComfyUI 自带的 /view 只服务 input/output/temp 目录下的文件，无法展示任意绝对路径。
try:
    from aiohttp import web
    from server import PromptServer

    _H3_IMAGE_ROUTE = "/tt/h3/image"

    def _is_route_registered(server, method, path):
        try:
            for d in server.routes._items:
                if getattr(d, "method", None) == method and getattr(d, "path", None) == path:
                    return True
        except Exception:
            pass
        return False

    if not _is_route_registered(PromptServer.instance, "GET", _H3_IMAGE_ROUTE):
        @PromptServer.instance.routes.get(_H3_IMAGE_ROUTE)
        async def _serve_h3_image(request):
            path = request.query.get("path", "")
            if not path or not os.path.isfile(path):
                return web.Response(status=404)
            try:
                img = Image.open(path)
                img = ImageOps.exif_transpose(img)
                img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return web.Response(body=buf.getvalue(), content_type="image/png")
            except Exception:
                return web.Response(status=500)
except Exception:
    pass


def _load_image_tensor(path):
    """加载单张图片为 (1,H,W,3) 的 RGB 张量，不做任何缩放/压缩处理。"""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None, ...]


class TTH3MultiImageLoader:
    """MiniMax H3 多图加载器：固定 9 张图片输出。
    支持在输入框粘贴图片绝对路径（每行一张，最多 9 张），或通过 Upload 上传（最多 9 张）。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image_paths": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": True,
                        "placeholder": "粘贴图片绝对路径，每行一张，最多 9 张",
                    },
                ),
            },
        }

    RETURN_TYPES = ("IMAGE",) * MAX_IMAGES
    RETURN_NAMES = tuple(f"image_{i + 1}" for i in range(MAX_IMAGES))
    FUNCTION = "load_images"
    CATEGORY = "TT_MiniMaxH3/图片处理"

    def load_images(self, image_paths):
        paths = [p.strip() for p in (image_paths or "").splitlines() if p.strip()][:MAX_IMAGES]
        results = []
        for p in paths:
            full = p
            if not os.path.exists(full):
                full = os.path.join(folder_paths.get_input_directory(), p)
            if not os.path.exists(full):
                print(f"[TTH3MultiImageLoader] 图片不存在，已跳过: {p}")
                continue
            try:
                results.append(_load_image_tensor(full))
            except Exception as e:
                print(f"[TTH3MultiImageLoader] 图片加载失败 {p}: {e}")

        missing = MAX_IMAGES - len(results)
        outs = results + [torch.zeros((1, 64, 64, 3), dtype=torch.float32)] * missing
        return tuple(outs)
