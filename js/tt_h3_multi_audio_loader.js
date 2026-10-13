import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const TARGET_CLASS = "TTH3MultiAudioLoader";
const MAX_AUDIOS = 3;
const PATHS_WIDGET = "audio_paths";

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
        .slice(0, MAX_AUDIOS);
}

function isAbsolutePath(p) {
    return /^[a-zA-Z]:[\\/]/.test(p) || p.startsWith("/") || p.startsWith("\\");
}

function audioSrc(path) {
    if (isAbsolutePath(path)) {
        return `/tt/h3/audio?path=${encodeURIComponent(path)}`;
    }
    return `/api/view?filename=${encodeURIComponent(path)}&type=input`;
}

function formatTime(secs) {
    if (isNaN(secs) || secs < 0) secs = 0;
    if (secs < 60) return secs.toFixed(1) + "s";
    const m = Math.floor(secs / 60);
    const s = secs % 60;
    return `${m}:${s.toFixed(1).padStart(4, "0")}`;
}

// 创建单路音频槽位（index 1..3，固定绑定到 start_time_{index}/end_time_{index}）
function createSlot(node, index) {
    const startWidget = getWidget(node, `start_time_${index}`);
    const endWidget = getWidget(node, `end_time_${index}`);

    const panel = document.createElement("div");
    panel.dataset.slotIndex = String(index);
    panel.draggable = true;
    panel.style.cssText = `
        display: flex;
        flex-direction: column;
        gap: 4px;
        background: rgba(0, 0, 0, 0.35);
        border: 1px solid rgba(255, 255, 255, 0.06);
        border-radius: 6px;
        padding: 6px;
        cursor: grab;
        transition: opacity 0.15s, border-color 0.15s;
    `;

    // 第1行：开始/时长 数字输入（与滑块联动）
    const trimRow = document.createElement("div");
    trimRow.style.cssText = "display:flex;gap:6px;align-items:center;";
    const makeNumInput = (label) => {
        const wrap = document.createElement("label");
        wrap.style.cssText = "display:flex;align-items:center;gap:4px;flex:1;min-width:0;font-size:10px;color:#aaa;";
        const span = document.createElement("span");
        span.textContent = label;
        const inp = document.createElement("input");
        inp.type = "number";
        inp.min = 0;
        inp.step = 0.01;
        inp.style.cssText = "flex:1;min-width:0;background:#141414;color:#e8e8e8;border:1px solid #353545;border-radius:4px;padding:2px 4px;font-size:11px;box-sizing:border-box;";
        wrap.appendChild(span);
        wrap.appendChild(inp);
        return { wrap, inp };
    };
    const startInput = makeNumInput("开始");
    const durInput = makeNumInput("时长");
    trimRow.appendChild(startInput.wrap);
    trimRow.appendChild(durInput.wrap);

    const header = document.createElement("div");
    header.style.cssText = "display:flex;justify-content:space-between;align-items:center;gap:6px;";
    const grip = document.createElement("span");
    grip.textContent = "⠿";
    grip.style.cssText = "font-size:10px;color:#666;user-select:none;";
    const nameEl = document.createElement("span");
    nameEl.textContent = `音频 ${index} · 未选择`;
    nameEl.style.cssText = "font-size:11px;color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;";
    const badge = document.createElement("span");
    badge.textContent = "整段";
    badge.style.cssText = "font-size:10px;color:#38bdf8;font-weight:bold;background:rgba(56,189,248,0.1);padding:2px 6px;border-radius:4px;white-space:nowrap;";
    header.appendChild(grip);
    header.appendChild(nameEl);
    header.appendChild(badge);

    const audioEl = document.createElement("audio");
    audioEl.controls = true;
    audioEl.preload = "metadata";
    audioEl.style.cssText = "width:100%;height:34px;outline:none;";

    // 修剪滑块
    const sliderBox = document.createElement("div");
    sliderBox.dataset.slotSlider = "";
    sliderBox.style.cssText = `
        position: relative;
        width: 100%;
        height: 18px;
        background: #111;
        border-radius: 4px;
        cursor: pointer;
        user-select: none;
        box-shadow: inset 0 1px 3px rgba(0,0,0,0.5);
    `;
    const fill = document.createElement("div");
    fill.style.cssText = "position:absolute;height:100%;background:rgba(14,165,233,0.35);pointer-events:none;";
    sliderBox.appendChild(fill);
    const makeHandle = () => {
        const h = document.createElement("div");
        h.style.cssText = "position:absolute;top:0;width:8px;height:100%;background:#38bdf8;transform:translateX(-50%);pointer-events:none;box-shadow:0 0 4px rgba(0,0,0,0.8);border-radius:2px;";
        return h;
    };
    const startHandle = makeHandle();
    const endHandle = makeHandle();
    sliderBox.appendChild(startHandle);
    sliderBox.appendChild(endHandle);

    const timeRow = document.createElement("div");
    timeRow.style.cssText = "display:flex;justify-content:space-between;font-size:10px;color:#888;";
    const startLabel = document.createElement("span");
    const endLabel = document.createElement("span");
    timeRow.appendChild(startLabel);
    timeRow.appendChild(endLabel);

    panel.appendChild(trimRow);
    panel.appendChild(header);
    panel.appendChild(audioEl);
    panel.appendChild(sliderBox);
    panel.appendChild(timeRow);

    let duration = 0;
    let dragging = null;
    let dragOffset = 0;
    let dragSelectionWidth = 0;

    const getStart = () => Math.max(0, parseFloat(startWidget?.value) || 0);
    const getEnd = () => {
        const e = parseFloat(endWidget?.value) || 0;
        return e > 0 ? e : duration;
    };
    const setStart = (v) => { if (startWidget) startWidget.value = parseFloat(Math.max(0, v).toFixed(2)); };
    const setEnd = (v) => { if (endWidget) endWidget.value = parseFloat(Math.max(0, v).toFixed(2)); };

    function updateUI(syncPlayer, updateInputs = true) {
        if (!duration) return;
        let s = Math.min(getStart(), duration);
        let e = Math.min(getEnd(), duration);
        if (e <= 0) e = duration;
        if (s > e) s = e;
        const sPct = (s / duration) * 100;
        const ePct = (e / duration) * 100;
        startHandle.style.left = `${sPct}%`;
        endHandle.style.left = `${ePct}%`;
        fill.style.left = `${sPct}%`;
        fill.style.width = `${ePct - sPct}%`;
        badge.textContent = (s <= 0 && e >= duration) ? "整段" : `${formatTime(s)} – ${formatTime(e)}`;
        startLabel.textContent = formatTime(s);
        endLabel.textContent = formatTime(e);
        if (updateInputs) {
            startInput.inp.value = parseFloat(s.toFixed(2));
            durInput.inp.value = parseFloat((e - s).toFixed(2));
        }
        if (syncPlayer && audioEl.readyState >= 1) audioEl.currentTime = s;
    }

    function setPath(path) {
        if (!path) {
            nameEl.textContent = `音频 ${index} · 未选择`;
            audioEl.removeAttribute("src");
            audioEl.load();
            return;
        }
        nameEl.textContent = `音频 ${index} · ${path.split(/[\\/]/).pop()}`;
        audioEl.src = audioSrc(path);
        audioEl.load();
    }

    audioEl.onloadedmetadata = () => {
        duration = audioEl.duration || 0;
        let e = getEnd();
        if (e === 0 || e > duration) setEnd(duration);
        if (getStart() > duration) setStart(0);
        updateUI();
    };

    audioEl.ontimeupdate = () => {
        if (dragging || !duration) return;
        const e = getEnd() || duration;
        if (audioEl.currentTime >= e) {
            audioEl.pause();
            audioEl.currentTime = getStart();
        }
    };

    audioEl.onplay = () => {
        const s = getStart();
        const e = getEnd() || duration;
        if (audioEl.currentTime < s || audioEl.currentTime >= e) audioEl.currentTime = s;
    };

    sliderBox.onpointerdown = (e) => {
        if (!duration) return;
        const rect = sliderBox.getBoundingClientRect();
        const x = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
        const val = (x / rect.width) * duration;
        const s = getStart();
        const eVal = getEnd() || duration;
        const handleTolerance = (10 / rect.width) * duration;

        if (val > s + handleTolerance && val < eVal - handleTolerance) {
            dragging = "center";
            dragOffset = val - s;
            dragSelectionWidth = eVal - s;
        } else if (Math.abs(val - s) < Math.abs(val - eVal)) {
            dragging = "start";
            setStart(Math.min(val, eVal));
        } else {
            dragging = "end";
            setEnd(Math.max(val, s));
        }
        updateUI(true);
        app.graph?.setDirtyCanvas(true, false);
        sliderBox.setPointerCapture(e.pointerId);
    };

    sliderBox.onpointermove = (e) => {
        if (!dragging || !duration) return;
        const rect = sliderBox.getBoundingClientRect();
        const x = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
        const val = (x / rect.width) * duration;
        if (dragging === "start") {
            setStart(Math.min(val, getEnd() || duration));
        } else if (dragging === "end") {
            setEnd(Math.max(val, getStart()));
        } else if (dragging === "center") {
            let newStart = val - dragOffset;
            let newEnd = newStart + dragSelectionWidth;
            if (newStart < 0) {
                newStart = 0;
                newEnd = dragSelectionWidth;
            } else if (newEnd > duration) {
                newEnd = duration;
                newStart = duration - dragSelectionWidth;
            }
            setStart(newStart);
            setEnd(newEnd);
        }
        updateUI(true);
        app.graph?.setDirtyCanvas(true, false);
    };

    sliderBox.onpointerup = (e) => {
        dragging = null;
        try { sliderBox.releasePointerCapture(e.pointerId); } catch (_) { }
    };

    // --- 数字输入与滑块联动（开始 + 时长，change 提交式，避免打字时被改写） ---
    startInput.inp.addEventListener("change", () => {
        let v = parseFloat(startInput.inp.value);
        if (isNaN(v)) v = 0;
        let d = parseFloat(durInput.inp.value);
        if (isNaN(d) || d < 0) d = 0;
        if (duration) {
            if (d > duration) { d = duration; durInput.inp.value = d.toFixed(2); }
            if (v > duration - d) v = Math.max(0, duration - d); // 夹紧：起点+时长不超过文件末尾
            v = Math.min(v, duration);
        }
        setStart(v);
        setEnd(v + d);
        updateUI(false, true);
        startInput.inp.value = parseFloat(v.toFixed(2));
    });

    durInput.inp.addEventListener("change", () => {
        let d = parseFloat(durInput.inp.value);
        if (isNaN(d) || d < 0) d = 0;
        if (duration && d > duration) { d = duration; durInput.inp.value = d.toFixed(2); }
        let s = getStart();
        if (duration && s + d > duration) {
            s = Math.max(0, duration - d); // 超出末尾时反推起点，保证时长精确
            setStart(s);
        }
        setEnd(s + d);
        updateUI(false, true);
        durInput.inp.value = parseFloat(d.toFixed(2));
    });

    return { panel, index, setPath, updateUI };
}

app.registerExtension({
    name: "Comfy.TTH3MultiAudioLoader",
    async nodeCreated(node) {
        if (node.comfyClass !== TARGET_CLASS) return;

        const pathsWidget = getWidget(node, PATHS_WIDGET);
        if (!pathsWidget) return;

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

        const topBar = document.createElement("div");
        topBar.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;align-items:center;";
        const uploadBtn = document.createElement("button");
        uploadBtn.innerText = "Upload Audio";
        uploadBtn.style.cssText = "background:#3a3f4b;color:#fff;border:1px solid #5a5f6b;padding:3px 8px;border-radius:4px;cursor:pointer;font-size:10px;";
        const removeAllBtn = document.createElement("button");
        removeAllBtn.innerText = "Remove All";
        removeAllBtn.style.cssText = "background:#cc2222;color:#fff;border:1px solid #aa1111;padding:3px 8px;border-radius:4px;cursor:pointer;font-size:10px;";
        const countLabel = document.createElement("span");
        countLabel.style.cssText = "color:#aaa;font-size:10px;margin-left:auto;user-select:none;";
        topBar.appendChild(uploadBtn);
        topBar.appendChild(removeAllBtn);
        topBar.appendChild(countLabel);

        const textarea = document.createElement("textarea");
        textarea.placeholder = "粘贴音频绝对路径，每行一个（最多 3 个）";
        textarea.style.cssText = `
            width: 100%;
            height: 56px;
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

        const fileInput = document.createElement("input");
        fileInput.type = "file";
        fileInput.multiple = true;
        fileInput.accept = "audio/*,video/*";
        fileInput.style.display = "none";

        container.appendChild(topBar);
        container.appendChild(textarea);
        container.appendChild(fileInput);

        // 3 路音频槽位（固定位置 1..3，数据驱动渲染）
        const slots = [1, 2, 3].map((i) => {
            const slot = createSlot(node, i);
            container.appendChild(slot.panel);
            return slot;
        });

        const domWidget = typeof node.addDOMWidget === "function"
            ? node.addDOMWidget("h3_multi_audio_loader_ui", "h3_multi_audio_loader_ui", container, {
                serialize: false,
                hideOnZoom: false,
                getValue: () => "",
                setValue: () => {},
                getMinHeight: () => 340,
            })
            : null;
        if (!node.__autoMvH3Configured) node.size = [320, 600];

        // --- 状态与渲染 ---
        const currentPaths = () => parsePaths(pathsWidget?.value);

        function setWidgetValue(text) {
            if (!pathsWidget) return;
            pathsWidget.value = text;
            if (pathsWidget.inputEl) pathsWidget.inputEl.value = text;
            if (typeof pathsWidget.callback === "function") pathsWidget.callback(text);
        }

        function refresh() {
            const paths = currentPaths();
            textarea.value = paths.join("\n");
            countLabel.innerText = `${paths.length}/${MAX_AUDIOS}`;
            slots.forEach((slot, i) => {
                slot.setPath(paths[i]);
                slot.updateUI();
            });
            app.graph?.setDirtyCanvas(true, true);
        }

        textarea.addEventListener("input", () => {
            setWidgetValue(parsePaths(textarea.value).join("\n"));
        });

        // --- 上传（最多 3 个） ---
        async function handleFiles(files) {
            const allowed = MAX_AUDIOS - currentPaths().length;
            if (allowed <= 0) {
                alert("最多 3 个音频");
                return;
            }
            const uploaded = [];
            for (const file of Array.from(files).slice(0, allowed)) {
                const body = new FormData();
                body.append("image", file);
                body.append("type", "input");
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
                const next = currentPaths().concat(uploaded).slice(0, MAX_AUDIOS);
                setWidgetValue(next.join("\n"));
            }
        }

        uploadBtn.onclick = () => fileInput.click();
        removeAllBtn.onclick = () => setWidgetValue("");
        fileInput.onchange = (e) => {
            handleFiles(e.target.files);
            e.target.value = "";
        };

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

        // --- 拖拽排序（数据重排：路径与各自修剪值一起跟随槽位移动） ---
        let draggedSlot = null;
        let dropTarget = null;

        function applyOrder(srcIdx, dstIdx) {
            if (srcIdx === dstIdx) return;
            const oldPaths = currentPaths();
            const starts = [1, 2, 3].map((i) => parseFloat(getWidget(node, `start_time_${i}`)?.value) || 0);
            const ends = [1, 2, 3].map((i) => parseFloat(getWidget(node, `end_time_${i}`)?.value) || 0);
            // 把 src 位置的数据移到 dst 位置
            const order = [1, 2, 3].filter((i) => i !== srcIdx);
            order.splice(dstIdx - 1, 0, srcIdx);
            const newPaths = order.map((idx) => oldPaths[idx - 1]);
            order.forEach((idx, p) => {
                const sw = getWidget(node, `start_time_${p + 1}`);
                const ew = getWidget(node, `end_time_${p + 1}`);
                if (sw) sw.value = starts[idx - 1];
                if (ew) ew.value = ends[idx - 1];
            });
            setWidgetValue(newPaths.join("\n"));
        }

        slots.forEach((slot) => {
            const panel = slot.panel;
            panel.addEventListener("contextmenu", (e) => e.stopPropagation());
            // 从输入框/播放器/滑块处按下时禁用拖拽，保证输入框可正常选择数值
            panel.addEventListener("mousedown", (e) => {
                panel.draggable = !e.target.closest("input, audio, [data-slot-slider]");
            });
            panel.ondragstart = (e) => {
                draggedSlot = slot;
                dropTarget = null;
                e.dataTransfer.setData("text/plain", String(slot.index));
                e.dataTransfer.effectAllowed = "move";
                setTimeout(() => {
                    if (draggedSlot === slot) {
                        panel.style.opacity = "0.4";
                        panel.style.border = "2px dashed #666";
                    }
                }, 0);
            };
            panel.ondragover = (e) => {
                e.preventDefault();
                e.stopPropagation();
                if (!draggedSlot || draggedSlot === slot) return;
                dropTarget = slot;
                slot.panel.style.borderColor = "#38bdf8";
            };
            panel.ondragleave = () => {
                if (dropTarget === slot) {
                    dropTarget = null;
                    slot.panel.style.borderColor = "rgba(255,255,255,0.06)";
                }
            };
            panel.ondrop = (e) => {
                e.preventDefault();
                e.stopPropagation();
                slot.panel.style.borderColor = "rgba(255,255,255,0.06)";
                if (draggedSlot) applyOrder(draggedSlot.index, slot.index);
            };
            panel.ondragend = () => {
                if (draggedSlot) {
                    draggedSlot.panel.style.opacity = "1";
                    draggedSlot.panel.style.borderColor = "rgba(255,255,255,0.06)";
                }
                draggedSlot = null;
                dropTarget = null;
            };
        });

        // 外部修改（工作流加载/保存回填）时同步 UI
        const oldCallback = pathsWidget.callback;
        pathsWidget.callback = (v) => {
            if (oldCallback) oldCallback.apply(pathsWidget, [v]);
            refresh();
        };

        // 隐藏全部标准 widget
        hideWidget(pathsWidget);
        for (let i = 1; i <= MAX_AUDIOS; i++) {
            hideWidget(getWidget(node, `start_time_${i}`));
            hideWidget(getWidget(node, `end_time_${i}`));
        }

        refresh();
        setTimeout(() => refresh(), 100);
    },
});
