import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const TARGET_CLASS = "TTH3MultiImageLoader";
const MAX_IMAGES = 9;
const PATHS_WIDGET = "image_paths";

function getWidget(node, name) {
    return (node.widgets || []).find((w) => w && w.name === name);
}

function hideWidget(widget) {
    if (!widget || widget.__autoMvHidden) return;
    widget.__autoMvHidden = true;
    widget.type = "hidden";
    widget.hidden = true;
    widget.computeSize = () => [0, -4];
}

function parsePaths(text) {
    return String(text || "")
        .split(/\r?\n/)
        .map((s) => s.trim())
        .filter(Boolean)
        .slice(0, MAX_IMAGES);
}

function isAbsolutePath(p) {
    // Windows 盘符路径（E:\... 或 E:/...）、根路径 / 或 \\ 开头视为绝对路径
    return /^[a-zA-Z]:[\\/]/.test(p) || p.startsWith("/") || p.startsWith("\\");
}

function previewSrc(path) {
    if (isAbsolutePath(path)) {
        return `/tt/h3/image?path=${encodeURIComponent(path)}`;
    }
    return `/api/view?filename=${encodeURIComponent(path)}&type=input`;
}

app.registerExtension({
    name: "Comfy.TTH3MultiImageLoader",
    async nodeCreated(node) {
        if (node.comfyClass !== TARGET_CLASS) return;

        const pathsWidget = getWidget(node, PATHS_WIDGET);
        if (!pathsWidget) return;

        // 拖拽排序状态（参考 YusuMultiImageLoader）
        let draggedNode = null;
        let lastSwapX = 0;
        let lastSwapY = 0;
        let lastSwapTime = 0;

        // --- UI 容器 ---
        const container = document.createElement("div");
        container.style.cssText = `
            width: 100%;
            box-sizing: border-box;
            background: #1f1f1f;
            border: 1px solid #353545;
            border-radius: 6px;
            padding: 8px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            pointer-events: auto;
        `;

        // 顶部按钮栏
        const topBar = document.createElement("div");
        topBar.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;align-items:center;";
        const uploadBtn = document.createElement("button");
        uploadBtn.innerText = "Upload Images";
        uploadBtn.style.cssText = "background:#3a3f4b;color:#fff;border:1px solid #5a5f6b;padding:3px 8px;border-radius:4px;cursor:pointer;font-size:10px;";
        const removeAllBtn = document.createElement("button");
        removeAllBtn.innerText = "Remove All";
        removeAllBtn.style.cssText = "background:#cc2222;color:#fff;border:1px solid #aa1111;padding:3px 8px;border-radius:4px;cursor:pointer;font-size:10px;";
        const countLabel = document.createElement("span");
        countLabel.style.cssText = "color:#aaa;font-size:10px;margin-left:auto;user-select:none;";
        topBar.appendChild(uploadBtn);
        topBar.appendChild(removeAllBtn);
        topBar.appendChild(countLabel);

        // 路径输入框（粘贴图片绝对路径，每行一张）
        const textarea = document.createElement("textarea");
        textarea.placeholder = "粘贴图片绝对路径，每行一张（最多 9 张）";
        textarea.style.cssText = `
            width: 100%;
            height: 80px;
            box-sizing: border-box;
            resize: vertical;
            background: #141414;
            color: #e8e8e8;
            border: 1px solid #353545;
            border-radius: 4px;
            padding: 6px;
            font-size: 11px;
            font-family: Consolas, Menlo, monospace;
            line-height: 1.5;
        `;

        // 预览网格（最多 9 张，3 列）
        const grid = document.createElement("div");
        grid.style.cssText = `
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 6px;
            width: 100%;
        `;

        const fileInput = document.createElement("input");
        fileInput.type = "file";
        fileInput.multiple = true;
        fileInput.accept = "image/*";
        fileInput.style.display = "none";

        container.appendChild(topBar);
        container.appendChild(textarea);
        container.appendChild(grid);
        container.appendChild(fileInput);

        const domWidget = typeof node.addDOMWidget === "function"
            ? node.addDOMWidget("h3_multi_image_loader_ui", "h3_multi_image_loader_ui", container, {
                serialize: false,
                hideOnZoom: false,
                getValue: () => "",
                setValue: () => {},
                getMinHeight: () => 250,
            })
            : null;
        if (!node.__autoMvH3Configured) node.size = [300, 380];

        // --- 状态与渲染 ---
        function currentPaths() {
            return parsePaths(pathsWidget?.value);
        }

        function setWidgetValue(text) {
            if (!pathsWidget) return;
            pathsWidget.value = text;
            if (pathsWidget.inputEl) pathsWidget.inputEl.value = text;
            if (typeof pathsWidget.callback === "function") pathsWidget.callback(text);
        }

        function refresh() {
            const paths = currentPaths();
            textarea.value = paths.join("\n");
            countLabel.innerText = `${paths.length}/${MAX_IMAGES}`;
            grid.innerHTML = "";
            paths.forEach((path, index) => {
                const item = document.createElement("div");
                item.dataset.path = path;
                item.draggable = true;
                item.style.cssText = `
                    position: relative;
                    aspect-ratio: 1 / 1;
                    background: #000;
                    border-radius: 4px;
                    border: 1px solid #444;
                    overflow: hidden;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    cursor: grab;
                `;
                const img = document.createElement("img");
                img.src = previewSrc(path);
                img.style.cssText = "max-width:100%;max-height:100%;object-fit:contain;display:block;pointer-events:none;";
                img.draggable = false;
                img.onerror = () => { img.style.opacity = "0.3"; };

                const badge = document.createElement("div");
                badge.style.cssText = "position:absolute;bottom:0;left:0;background:rgba(0,0,0,.75);color:#fff;padding:1px 5px;font-size:10px;border-top-right-radius:4px;pointer-events:none;";
                badge.innerText = String(index + 1);

                const del = document.createElement("div");
                del.style.cssText = "position:absolute;top:0;right:0;background:#cc2222;color:#fff;width:32px;height:32px;display:flex;align-items:center;justify-content:center;font-size:22px;cursor:pointer;border-bottom-left-radius:4px;line-height:1;";
                del.innerText = "✕";
                del.onclick = (e) => {
                    e.stopPropagation();
                    const rest = currentPaths().filter((_, i) => i !== index);
                    setWidgetValue(rest.join("\n"));
                };

                // --- 拖拽排序（copy 自 YusuMultiImageLoader） ---
                item.addEventListener("contextmenu", (e) => e.stopPropagation());
                item.ondragstart = (e) => {
                    draggedNode = item;
                    e.dataTransfer.setData("text/plain", path);
                    e.dataTransfer.effectAllowed = "move";
                    setTimeout(() => {
                        if (draggedNode === item) {
                            item.style.background = "transparent";
                            item.style.border = "2px dashed #666";
                            Array.from(item.children).forEach((c) => { c.style.opacity = "0"; });
                        }
                    }, 0);
                };
                item.ondragend = () => {
                    if (draggedNode) {
                        draggedNode.style.background = "#000";
                        draggedNode.style.border = "1px solid #444";
                        Array.from(draggedNode.children).forEach((c) => { c.style.opacity = "1"; });
                    }
                    draggedNode = null;
                    const newPaths = Array.from(grid.children).map((n) => n.dataset.path);
                    if (newPaths.join("\n") !== currentPaths().join("\n")) {
                        setWidgetValue(newPaths.join("\n"));
                    }
                };
                item.ondragover = (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    if (!draggedNode || draggedNode === item) return;
                    const distMoved = Math.hypot(e.clientX - lastSwapX, e.clientY - lastSwapY);
                    if (Date.now() - lastSwapTime < 50 && distMoved < 5) return;
                    const itemRect = item.getBoundingClientRect();
                    const bufferX = itemRect.width * 0.25;
                    const bufferY = itemRect.height * 0.25;
                    if (e.clientX < itemRect.left + bufferX || e.clientX > itemRect.right - bufferX ||
                        e.clientY < itemRect.top + bufferY || e.clientY > itemRect.bottom - bufferY) {
                        return;
                    }
                    const items = Array.from(grid.children);
                    const draggedIdx = items.indexOf(draggedNode);
                    const targetIdx = items.indexOf(item);
                    if (draggedIdx < targetIdx) {
                        grid.insertBefore(draggedNode, item.nextSibling);
                    } else {
                        grid.insertBefore(draggedNode, item);
                    }
                    lastSwapX = e.clientX;
                    lastSwapY = e.clientY;
                    lastSwapTime = Date.now();
                };
                item.ondrop = (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                };

                item.appendChild(img);
                item.appendChild(badge);
                item.appendChild(del);
                grid.appendChild(item);
            });

            app.graph?.setDirtyCanvas(true, true);
        }

        // --- 上传（最多 9 张） ---
        async function handleFiles(files) {
            const allowed = MAX_IMAGES - currentPaths().length;
            if (allowed <= 0) {
                alert("最多上传 9 张图片");
                return;
            }
            const uploaded = [];
            for (const file of Array.from(files).slice(0, allowed)) {
                const body = new FormData();
                body.append("image", file);
                try {
                    const resp = await api.fetchApi("/upload/image", { method: "POST", body });
                    if (resp.status === 200) {
                        const data = await resp.json();
                        let name = data.name;
                        if (data.subfolder) name = data.subfolder + "/" + name;
                        uploaded.push(name);
                    }
                } catch (e) {
                    console.error("Upload error", e);
                }
            }
            if (uploaded.length > 0) {
                const next = currentPaths().concat(uploaded).slice(0, MAX_IMAGES);
                setWidgetValue(next.join("\n"));
            }
        }

        uploadBtn.onclick = () => fileInput.click();
        removeAllBtn.onclick = () => setWidgetValue("");
        fileInput.onchange = (e) => {
            handleFiles(e.target.files);
            e.target.value = "";
        };

        // 拖拽图片到节点
        container.ondragover = (e) => {
            e.preventDefault();
            e.stopPropagation();
            container.style.borderColor = "#4CAF50";
        };
        container.ondragleave = (e) => {
            e.preventDefault();
            e.stopPropagation();
            container.style.borderColor = "#353545";
        };
        container.ondrop = (e) => {
            e.preventDefault();
            e.stopPropagation();
            container.style.borderColor = "#353545";
            if (e.dataTransfer.files && e.dataTransfer.files.length > 0) handleFiles(e.dataTransfer.files);
        };

        // 粘贴剪贴板中的图片文件（Ctrl+V 且节点被选中时）
        const pasteHandler = (e) => {
            if (!app.canvas?.selected_nodes || !app.canvas.selected_nodes[node.id]) return;
            const items = e.clipboardData?.items;
            if (!items) return;
            const files = [];
            for (let i = 0; i < items.length; i++) {
                if (items[i].kind === "file" && items[i].type.startsWith("image/")) {
                    files.push(items[i].getAsFile());
                }
            }
            if (files.length > 0) {
                e.preventDefault();
                e.stopImmediatePropagation();
                handleFiles(files);
            }
        };
        document.addEventListener("paste", pasteHandler, { capture: true });

        const origOnRemoved = node.onRemoved;
        node.onRemoved = function () {
            document.removeEventListener("paste", pasteHandler, { capture: true });
            if (origOnRemoved) origOnRemoved.apply(this, arguments);
        };

        // 外部修改（工作流加载/保存回填）时同步 UI
        const oldCallback = pathsWidget.callback;
        pathsWidget.callback = (v) => {
            if (oldCallback) oldCallback.apply(pathsWidget, [v]);
            refresh();
        };

        // 统一使用 refresh() 渲染，避免两套逻辑
        textarea.addEventListener("input", () => {
            setWidgetValue(parsePaths(textarea.value).join("\n"));
        });

        hideWidget(pathsWidget);

        refresh();
        setTimeout(() => refresh(), 100);
    },
});
