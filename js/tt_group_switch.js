import { app } from "../../scripts/app.js";

const TARGET_CLASS = "TTGroupSwitch";
const COUNT_WIDGET = "开启数量";

let _gsCounter = 0;

app.registerExtension({
    name: "tt.group_switch",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== TARGET_CLASS) return;

        const origCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            origCreated?.apply(this, arguments);
            buildGroupSwitchUI(this);
        };

        const origConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function (info) {
            origConfigure?.apply(this, arguments);
            if (this._ttGsSync) this._ttGsSync();
        };

        const origGetExtra = nodeType.prototype.getExtraMenuOptions;
        nodeType.prototype.getExtraMenuOptions = function (canvas, options) {
            origGetExtra?.apply(this, arguments);
            options.unshift({
                content: "🎚  编组开关 设置",
                callback: () => {
                    if (this._ttGsShowSettings) {
                        this._ttGsShowSettings(
                            Math.round(innerWidth  / 2 - 130),
                            Math.round(innerHeight / 2 - 200)
                        );
                    }
                }
            });
        };
    },
});


function buildGroupSwitchUI(node) {

    const _uid = "gs_" + (++_gsCounter);
    const _styleId = "tt-gs-dyn-" + _uid;

    let filter      = "";
    let activeSet   = null;
    let nameColor   = null;
    let gsDisable   = false;
    let selfChanging = false;
    let dirty        = true;
    let sortOrder    = "position";
    let colorFilter  = "none";
    let prevVisibleTitles = new Set();
    let uiScale      = 1.0;

    if (node.properties) {
        if (node.properties.tt_gs_filter       != null) filter      = node.properties.tt_gs_filter;
        if (node.properties.tt_gs_active_set   != null) activeSet   = node.properties.tt_gs_active_set;
        if (node.properties.tt_gs_name_color   != null) nameColor   = node.properties.tt_gs_name_color;
        if (node.properties.tt_gs_disable      != null) gsDisable   = !!node.properties.tt_gs_disable;
        if (node.properties.tt_gs_sort_order   != null) sortOrder   = node.properties.tt_gs_sort_order;
        if (node.properties.tt_gs_color_filter != null) colorFilter = node.properties.tt_gs_color_filter;
        if (node.properties.tt_gs_ui_scale     != null) uiScale     = Number(node.properties.tt_gs_ui_scale) || 1.0;
    }

    function save() {
        node.properties = node.properties || {};
        node.properties.tt_gs_filter       = filter;
        node.properties.tt_gs_active_set   = activeSet;
        node.properties.tt_gs_name_color   = nameColor;
        node.properties.tt_gs_disable      = gsDisable;
        node.properties.tt_gs_sort_order   = sortOrder;
        node.properties.tt_gs_color_filter = colorFilter;
        node.properties.tt_gs_ui_scale     = uiScale;
    }

    function _ttGsSync() {
        filter      = (node.properties && node.properties.tt_gs_filter)      || "";
        activeSet   = (node.properties && node.properties.tt_gs_active_set)  || null;
        nameColor   = (node.properties && node.properties.tt_gs_name_color)  || null;
        gsDisable   = (node.properties && node.properties.tt_gs_disable)     || false;
        sortOrder   = (node.properties && node.properties.tt_gs_sort_order)  || "position";
        colorFilter = (node.properties && node.properties.tt_gs_color_filter)|| "none";
        uiScale     = (node.properties && node.properties.tt_gs_ui_scale)    || 1.0;
        lastSig = "";
        lastStateSig = "";
        prevVisibleTitles = new Set();
        _lastAppliedScale = -1;
        dirty = true;
        refresh(true);
    }


    const countWidget = (node.widgets || []).find(w => w && w.name === COUNT_WIDGET);

    function currentCount() {
        if (!countWidget) return 0;
        const v = parseInt(countWidget.value, 10);
        return Number.isFinite(v) && v > 0 ? v : 0;
    }

    function setCount(n) {
        if (!countWidget) return;
        n = Math.max(0, n | 0);
        if (countWidget.value !== n) countWidget.value = n;
    }

    if (countWidget) {
        const origCb = countWidget.callback;
        countWidget.callback = function () {
            const r = origCb ? origCb.apply(this, arguments) : undefined;
            applyCount();
            return r;
        };
    }


    const BASE_ROW_H     = 34;
    const BASE_PAD_X     = 16;
    const BASE_ROW_PAD_L = 26;
    const BASE_ROW_PAD_R = 16;
    const BASE_TOGGLE_W  = 67;
    const BASE_TOGGLE_H  = 26;
    const BASE_KNOB_R    = 8.8;
    const BASE_FONT_SIZE = 20;
    const BASE_BORDER_R  = 13;

    const HEADER_H = 14;
    const ROW_GAP  = 15;

    function S(base) { return Math.round(base * uiScale); }


    function calcWidth() {
        const fS  = S(BASE_FONT_SIZE);
        const pX  = S(BASE_PAD_X);
        const rPL = S(BASE_ROW_PAD_L);
        const rPR = S(BASE_ROW_PAD_R);
        const tW  = S(BASE_TOGGLE_W);
        const w = pX * 2 + rPL + rPR + 10 + tW + 2 + 8 * fS;
        return Math.max(350, Math.round(w));
    }


    let _lastAppliedScale = -1;

    function buildScopedCSS() {
        const rH  = S(BASE_ROW_H);
        const pX  = S(BASE_PAD_X);
        const rPL = S(BASE_ROW_PAD_L);
        const rPR = S(BASE_ROW_PAD_R);
        const tW  = S(BASE_TOGGLE_W);
        const tH  = S(BASE_TOGGLE_H);
        const kR  = S(BASE_KNOB_R);
        const kD  = kR * 2;
        const fS  = S(BASE_FONT_SIZE);
        const bR  = S(BASE_BORDER_R);

        const knobPad  = Math.max(0, Math.round((tH - kD) / 2));
        const knobOffL = knobPad + 3;
        const knobOnL  = Math.max(knobOffL, tW - kD - knobPad - 3);

        return `
            .${_uid} {
                position: relative; width: 100%; box-sizing: border-box;
                overflow: hidden; user-select: none;
                pointer-events: auto;
                margin-top: 2px;
                padding-bottom: 10px;
            }
            .${_uid} .h3gs-header {
                height: ${HEADER_H}px; display: flex;
                justify-content: flex-end; align-items: center;
                padding: 0 6px;
            }
            .${_uid} .h3gs-gear {
                width: 16px; height: 16px; cursor: pointer;
                opacity: 0.6; transition: opacity .15s;
                display: flex; align-items: center; justify-content: center;
                pointer-events: auto;
            }
            .${_uid} .h3gs-gear:hover { opacity: 1; }
            .${_uid} .h3gs-gear svg { width: 13px; height: 13px; }
            .${_uid} .h3gs-empty {
                color: #888; text-align: center;
                padding: 20px 16px; font-size: 13px;
            }
            .${_uid} .h3gs-row {
                display: flex; align-items: center; justify-content: space-between;
                height: ${rH}px;
                margin: 0 ${pX}px ${ROW_GAP}px;
                padding: 0 ${rPR}px 0 ${rPL}px;
                background: #2B2F38; border: 1px solid #6E7581;
                border-radius: ${bR}px;
                cursor: pointer; box-sizing: border-box;
                transition: border-color .15s;
                pointer-events: auto;
            }
            .${_uid} .h3gs-row:hover { border-color: #8E95A1; }
            .${_uid} .h3gs-row:last-child { margin-bottom: 5px; }
            .${_uid} .h3gs-label {
                font-weight: bold; font-size: ${fS}px; flex: 1;
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
                margin-right: 10px; transition: opacity .15s;
            }
            .${_uid} .h3gs-toggle {
                width: ${tW}px; height: ${tH}px; border-radius: ${tH / 2}px;
                background: #606060; position: relative; flex-shrink: 0;
                transition: background .2s ease, box-shadow .2s ease;
            }
            .${_uid} .h3gs-knob {
                width: ${kD}px; height: ${kD}px; border-radius: 50%;
                background: rgb(128,128,128);
                position: absolute; top: ${knobPad}px; left: ${knobOffL}px;
                transition: left .2s ease, background .2s ease;
                box-shadow: 0 1px 4px rgba(0,0,0,0.3);
            }
            .${_uid} .h3gs-toggle.on .h3gs-knob {
                left: ${knobOnL}px;
                background: rgb(230,230,230);
            }
        `;
    }

    function updateDynamicStyles() {
        if (Math.abs(_lastAppliedScale - uiScale) < 0.001) return;
        _lastAppliedScale = uiScale;
        let s = document.getElementById(_styleId);
        if (!s) {
            s = document.createElement("style");
            s.id = _styleId;
            document.head.appendChild(s);
        }
        s.textContent = buildScopedCSS();
    }


    function forwardWheelToCanvas(e) {
        e.preventDefault();
        e.stopPropagation();
        try {
            const canvasEl = (app.canvas && app.canvas.canvas) || document.querySelector("canvas");
            if (canvasEl) {
                canvasEl.dispatchEvent(new WheelEvent("wheel", {
                    clientX: e.clientX, clientY: e.clientY,
                    deltaY: e.deltaY, deltaX: e.deltaX,
                    deltaMode: e.deltaMode, bubbles: true, cancelable: true,
                }));
            }
        } catch (_) {}
    }


    function gBounds(g) {
        if (g._bounding) return [...g._bounding];
        if (g.bounding)  return [...g.bounding];
        const p = g.pos || [0, 0], s = g.size || [0, 0];
        return [p[0], p[1], s[0], s[1]];
    }

    function getGroupColor(g) {
        if (g.color) {
            let c = g.color;
            if (typeof c === "number") {
                return "#" + c.toString(16).padStart(6, "0");
            }
            if (typeof c === "string" && c.startsWith("#") && (c.length === 4 || c.length === 7)) {
                if (c.length === 4) {
                    return "#" + c[1]+c[1] + c[2]+c[2] + c[3]+c[3];
                }
                return c;
            }
        }
        return "";
    }

    function nBounds(n) {
        const p = n.pos || [0, 0];
        const s = n.size || [100, 60];
        if (n.collapsed || n._collapsed) {
            const titleH = (typeof LiteGraph !== "undefined" && LiteGraph.NODE_TITLE_HEIGHT) || 30;
            let collapsedW = n._collapsed_width;
            if (!collapsedW || collapsedW <= 0) {
                const title = (typeof n.getTitle === "function" ? n.getTitle() : n.title) || "";
                collapsedW = Math.max(80, title.length * 7 + 50);
            }
            return [p[0], p[1], collapsedW, titleH];
        }
        return [p[0], p[1], s[0], s[1]];
    }

    function hit(a, b) {
        return !(a[0] + a[2] <= b[0] || a[0] >= b[0] + b[2] ||
                 a[1] + a[3] <= b[1] || a[1] >= b[1] + b[3]);
    }

    function inside(inner, outer) {
        return inner[0] >= outer[0] && inner[1] >= outer[1] &&
               inner[0] + inner[2] <= outer[0] + outer[2] &&
               inner[1] + inner[3] <= outer[1] + outer[3];
    }


    let _rgCache = null;

    function rawGroups(invalidate) {
        if (invalidate) { _rgCache = null; }
        if (_rgCache) return _rgCache;
        const g = app.graph;
        if (!g || !g._groups) { _rgCache = []; return _rgCache; }
        _rgCache = g._groups.map(gr => ({
            title:  (gr.title || "").trim() || "Unnamed",
            bounds: gBounds(gr),
            ref:    gr,
            color:  getGroupColor(gr),
        }));
        return _rgCache;
    }

    function visible(cachedAll) {
        let list = cachedAll || rawGroups();
        if (!cachedAll) list = list.slice();
        list = list.filter(g => collectNodes(g, list, cachedAll).length > 0);
        if (filter.trim()) {
            const kw = filter.trim().toLowerCase();
            list = list.filter(g => g.title.toLowerCase().includes(kw));
        }
        if (colorFilter && colorFilter !== "none") {
            list = list.filter(g => {
                if (colorFilter === "__transparent__") return !g.color;
                return g.color && g.color.toLowerCase() === colorFilter.toLowerCase();
            });
        }
        if (sortOrder === "position") {
            list.sort((a, b) => {
                const ax = a.bounds[0], ay = a.bounds[1];
                const bx = b.bounds[0], by = b.bounds[1];
                if (ay !== by) return ay - by;
                return ax - bx;
            });
        } else {
            list.sort((a, b) =>
                a.title.localeCompare(b.title, undefined, { sensitivity: "base" })
            );
        }
        return list;
    }

    function collectNodes(grp, allGroups, cachedAll) {
        const g = app.graph;
        if (!g || !g._nodes) return [];
        const all  = allGroups || rawGroups();
        const pb   = grp.bounds;
        const rects = [pb];
        all.forEach(ag => {
            if (ag.ref !== grp.ref && inside(ag.bounds, pb)) {
                rects.push(ag.bounds);
            }
        });
        return g._nodes.filter(n => {
            const nb = nBounds(n);
            return rects.some(r => hit(nb, r));
        });
    }


    function bypassGroup(grp, cachedAll) {
        collectNodes(grp, null, cachedAll).forEach(n => {
            if (gsDisable) { n.mode = 2; } else { n.mode = 4; }
        });
    }

    function restoreGroup(grp, cachedAll) {
        collectNodes(grp, null, cachedAll).forEach(n => {
            n.mode = 0;
            if (n.flags) n.flags.disabled = false;
        });
    }

    function isNodeActive(n) {
        return n.mode !== 4 && n.mode !== 2 && !n.flags?.disabled;
    }

    function getGroupState(grp, cachedAll) {
        const nodes = collectNodes(grp, null, cachedAll);
        if (nodes.length === 0) return true;
        const allActive   = nodes.every(n => isNodeActive(n));
        const allInactive = nodes.every(n => !isNodeActive(n));
        if (allActive)   return true;
        if (allInactive) return false;
        return null;
    }

    function computeStateSig(list, cachedAll) {
        return list.map(g => {
            const s = getGroupState(g, cachedAll);
            return g.title + ":" + (s === true ? "1" : s === false ? "0" : "m");
        }).join("\x00");
    }


    function syncExternalState(cachedAll) {
        if (!app.graph || !app.graph._nodes || app.graph._nodes.indexOf(node) < 0) return false;
        if (!Array.isArray(activeSet)) return false;

        let changed = false;
        rawGroups(true).forEach(g => {
            const gs = getGroupState(g, cachedAll);
            const isActive = activeSet.includes(g.title);
            if (gs === true && !isActive) {
                activeSet.push(g.title);
                changed = true;
            } else if (gs === false && isActive) {
                activeSet = activeSet.filter(t => t !== g.title);
                changed = true;
            }
        });
        if (changed) save();
        return changed;
    }


    function hexToRgba(hex, alpha) {
        if (!hex || hex.length < 7) return `rgba(120,120,120,${alpha})`;
        const r = parseInt(hex.slice(1, 3), 16);
        const g = parseInt(hex.slice(3, 5), 16);
        const b = parseInt(hex.slice(5, 7), 16);
        return `rgba(${r},${g},${b},${alpha})`;
    }


    const gearSVG = `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <circle cx="12" cy="12" r="3" fill="rgba(120,120,120,0.3)" stroke="rgba(153,153,153,0.6)" stroke-width="1"/>
        <path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.07.62-.07.94s.02.64.07.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58z"
            fill="rgba(120,120,120,0.25)" stroke="rgba(120,120,120,0.4)" stroke-width="1.2"/>
    </svg>`;

    const rootEl = document.createElement("div");
    rootEl.className = "h3gs " + _uid;
    rootEl.style.visibility = "hidden";

    rootEl.addEventListener("wheel", forwardWheelToCanvas, { passive: false });

    let _lastBuildSig = "";

    function calcHeight(cnt) {
        const rowH = S(BASE_ROW_H);
        const n = Math.max(cnt != null ? cnt : visible().length, 1);
        return HEADER_H + n * (rowH + ROW_GAP) - ROW_GAP + 10;
    }

    function leadingOnCount(list) {
        let c = 0;
        for (const g of list) {
            if (Array.isArray(activeSet) && activeSet.includes(g.title)) c++;
            else break;
        }
        return c;
    }

    function applyCount() {
        const list = visible(rawGroups(true));
        const n = Math.min(currentCount(), list.length);
        activeSet = list.slice(0, n).map(g => g.title);
        save();
        refresh(true);
    }

    function buildStatefulSig() {
        const list = visible();
        const effectiveColor = nameColor || "#a89961";
        let sig = list.map(g => g.title + (activeSet && activeSet.includes(g.title) ? ":1" : ":0")).join("\x00");
        sig += "|" + effectiveColor + "|" + uiScale;
        return sig;
    }

    function buildDom(force) {
        const sig = buildStatefulSig();
        if (sig === _lastBuildSig && !force) return;
        _lastBuildSig = sig;

        const list = visible();
        const effectiveColor = nameColor || "#a89961";

        rootEl.innerHTML = "";

        const header = document.createElement("div");
        header.className = "h3gs-header";
        const gear = document.createElement("div");
        gear.className = "h3gs-gear";
        gear.innerHTML = gearSVG;
        gear.title = "设置";
        gear.addEventListener("mousedown", (e) => {
            e.preventDefault(); e.stopPropagation();
            showSettings(e.clientX, e.clientY);
        });
        gear.addEventListener("pointerdown", (e) => e.stopPropagation());
        gear.addEventListener("wheel", forwardWheelToCanvas, { passive: false });
        header.appendChild(gear);
        rootEl.appendChild(header);

        if (!list.length) {
            const empty = document.createElement("div");
            empty.className = "h3gs-empty";
            empty.textContent = filter.trim() ? "无匹配的组" : "工作流中无编组或空的编组";
            rootEl.appendChild(empty);
        } else {
            const h = calcHeight(list.length);
            rootEl.style.minHeight = h + "px";
            rootEl.style.height    = h + "px";

            list.forEach((g) => {
                const isOn = !!(activeSet && activeSet.includes(g.title));

                const row = document.createElement("div");
                row.className = "h3gs-row";

                const label = document.createElement("div");
                label.className = "h3gs-label";
                label.style.color = effectiveColor;
                label.style.opacity = isOn ? "1" : "0.5";
                label.textContent = g.title;

                const toggle = document.createElement("div");
                toggle.className = "h3gs-toggle" + (isOn ? " on" : "");
                toggle.style.background = isOn ? effectiveColor : "#606060";
                toggle.style.boxShadow = isOn ? "0 0 8px " + hexToRgba(effectiveColor, 0.35) : "none";

                const knob = document.createElement("div");
                knob.className = "h3gs-knob";
                toggle.appendChild(knob);
                row.appendChild(label);
                row.appendChild(toggle);

                row.addEventListener("mousedown", (e) => {
                    e.preventDefault(); e.stopPropagation();
                    handleToggle(g.title);
                });
                row.addEventListener("pointerdown", (e) => e.stopPropagation());
                row.addEventListener("wheel", forwardWheelToCanvas, { passive: false });

                rootEl.appendChild(row);
            });
        }
    }


    const domWidget = node.addDOMWidget("tt_gs_ui", "GS_UI", rootEl, {
        serialize: false,
        hideOnZoom: false,
    });

    if (domWidget) {
        domWidget.computeSize = function () {
            return [calcWidth(), calcHeight()];
        };
    }

    function fitNodeSize() {
        const panelH = rootEl.offsetHeight || calcHeight();
        let other = 0;
        for (const w of node.widgets || []) {
            if (w === domWidget) continue;
            const c = w.computeSize ? w.computeSize(node.size ? node.size[0] : calcWidth()) : null;
            if (c && typeof c[1] === "number" && c[1] > 0) other += c[1];
        }
        const need = Math.ceil(other + panelH + 30);
        if (Math.abs(need - (node.size ? node.size[1] : 0)) > 2) {
            node.setSize?.([calcWidth(), need]);
        }
    }


    let lastSig = "";
    let lastStateSig = "";

    function refresh(forceApply) {
        const cachedAll = rawGroups(true);
        const list = visible(cachedAll);
        const currentVisibleTitles = new Set(list.map(g => g.title));
        const sig  = list.map(g => g.title).join("\x00");
        const listChanged = (sig !== lastSig);
        lastSig = sig;

        let stateChanged = false;

        if (!Array.isArray(activeSet)) {
            activeSet = [];
            const n = Math.min(currentCount(), list.length);
            for (let i = 0; i < n; i++) activeSet.push(list[i].title);
            stateChanged = true;
        } else {
            const allTitles = new Set(cachedAll.map(g => g.title));
            const before = activeSet.length;
            activeSet = activeSet.filter(t => allTitles.has(t));
            if (activeSet.length !== before) stateChanged = true;

            if (listChanged) {
                list.forEach(g => {
                    if (!prevVisibleTitles.has(g.title)) {
                        const gs = getGroupState(g, cachedAll);
                        const idx = activeSet.indexOf(g.title);
                        if (gs === true && idx < 0) {
                            activeSet.push(g.title);
                            stateChanged = true;
                        } else if (gs === false && idx >= 0) {
                            activeSet.splice(idx, 1);
                            stateChanged = true;
                        }
                    }
                });
            }
        }

        if (stateChanged) save();

        if (forceApply || stateChanged || listChanged) {
            selfChanging = true;
            try {
                list.forEach(g => {
                    if (activeSet.includes(g.title)) restoreGroup(g, cachedAll);
                    else                            bypassGroup(g, cachedAll);
                });
                try { app.graph.change(); } catch (_) {}
            } finally {
                selfChanging = false;
            }
        }

        prevVisibleTitles = currentVisibleTitles;

        setCount(leadingOnCount(list));
        updateDynamicStyles();
        buildDom(forceApply || stateChanged || listChanged);
        fitNodeSize();
    }


    function handleToggle(title) {
        if (!Array.isArray(activeSet)) activeSet = [];
        const idx = activeSet.indexOf(title);
        if (idx < 0) activeSet.push(title);
        else         activeSet.splice(idx, 1);
        save();
        refresh(true);
    }


    let _settingsCleanup = null;

    function showSettings(x, y) {
        if (_settingsCleanup) { _settingsCleanup(); _settingsCleanup = null; }

        let _initialSettingsScale = uiScale;
        let _needsNodeHeightRefresh = false;

        const overlay = document.createElement("div");
        overlay.id = "tt_gs_overlay";
        Object.assign(overlay.style, {
            position: "fixed", inset: "0", zIndex: "99998",
            background: "transparent", cursor: "default",
        });
        overlay.addEventListener("wheel", forwardWheelToCanvas, { passive: false });
        document.body.appendChild(overlay);

        const pop = document.createElement("div");
        pop.id = "tt_gs_pop";
        Object.assign(pop.style, {
            position: "fixed",
            left: Math.min(x, innerWidth  - 280) + "px",
            top:  Math.min(y, innerHeight - 620) + "px",
            background: "#2a2a2a", border: "1px solid #555",
            borderRadius: "8px", padding: "14px 18px",
            zIndex: "99999", minWidth: "250px",
            boxShadow: "0 4px 24px rgba(0,0,0,0.6)",
            color: "#e0e0e0", fontFamily: "inherit",
        });
        pop.addEventListener("wheel", forwardWheelToCanvas, { passive: false });

        function closePopup() {
            if (overlay.parentNode) overlay.remove();
            if (pop.parentNode)     pop.remove();
            document.removeEventListener("keydown", onEsc);
            document.removeEventListener("mousedown", closeColorPanel);
            _settingsCleanup = null;
            if (_needsNodeHeightRefresh) {
                _needsNodeHeightRefresh = false;
                fitNodeSize();
                app.graph.change();
            }
        }
        _settingsCleanup = closePopup;

        overlay.addEventListener("mousedown", (e) => {
            e.preventDefault(); e.stopPropagation(); closePopup();
        });
        function onEsc(e) { if (e.key === "Escape") closePopup(); }
        document.addEventListener("keydown", onEsc);

        function applyAll() {
            filter      = fInput.value;
            colorFilter = cFilter;
            nameColor   = cInput.value || null;
            gsDisable   = dRadioDisable.checked;
            sortOrder   = sSelect.value;
            const newScale = parseFloat(scaleInput.value) || 1.0;

            uiScale = newScale;
            updateDynamicStyles();

            if (Math.abs(newScale - _initialSettingsScale) > 0.001) {
                _needsNodeHeightRefresh = true;
            }

            save();
            refresh(true);
        }

        const titleEl = document.createElement("div");
        titleEl.textContent = "🎚  编组开关 设置";
        Object.assign(titleEl.style, {
            fontSize: "15px", fontWeight: "bold", marginBottom: "14px",
            color: "#e0e0e0", borderBottom: "1px solid #444", paddingBottom: "8px",
        });
        pop.appendChild(titleEl);

        const dLabel = document.createElement("div");
        dLabel.textContent = "路由控制";
        Object.assign(dLabel.style, { fontSize: "13px", fontWeight: "bold", marginBottom: "6px" });
        pop.appendChild(dLabel);

        const dRow = document.createElement("div");
        Object.assign(dRow.style, { display: "flex", alignItems: "center", gap: "20px", marginBottom: "14px" });

        const dRadioBypass = document.createElement("input");
        dRadioBypass.type = "radio"; dRadioBypass.name = "tt_gs_close_mode";
        dRadioBypass.value = "bypass"; dRadioBypass.checked = !gsDisable; dRadioBypass.style.cursor = "pointer";
        const dLblBypass = document.createElement("label");
        dLblBypass.style.cursor = "pointer"; dLblBypass.style.fontSize = "13px";
        dLblBypass.appendChild(dRadioBypass); dLblBypass.appendChild(document.createTextNode(" 绕过（ctrl+b）"));

        const dRadioDisable = document.createElement("input");
        dRadioDisable.type = "radio"; dRadioDisable.name = "tt_gs_close_mode";
        dRadioDisable.value = "disable"; dRadioDisable.checked = !!gsDisable; dRadioDisable.style.cursor = "pointer";
        const dLblDisable = document.createElement("label");
        dLblDisable.style.cursor = "pointer"; dLblDisable.style.fontSize = "13px";
        dLblDisable.appendChild(dRadioDisable); dLblDisable.appendChild(document.createTextNode(" 禁用（ctrl+m）"));

        dRow.appendChild(dLblBypass); dRow.appendChild(dLblDisable); pop.appendChild(dRow);
        dRadioBypass.addEventListener("change", applyAll);
        dRadioDisable.addEventListener("change", applyAll);

        const fLabel = document.createElement("div");
        fLabel.textContent = "关键词筛选";
        Object.assign(fLabel.style, { fontSize: "13px", fontWeight: "bold", marginBottom: "4px" });
        pop.appendChild(fLabel);

        const fInput = document.createElement("input");
        fInput.type = "text"; fInput.value = filter; fInput.placeholder = "留空 = 显示所有组";
        Object.assign(fInput.style, {
            width: "100%", padding: "5px 8px", fontSize: "13px",
            background: "#1a1a1a", border: "1px solid #555", borderRadius: "4px",
            color: "#e0e0e0", outline: "none", boxSizing: "border-box", marginBottom: "14px",
        });
        pop.appendChild(fInput);
        let filterTimer = null;
        fInput.addEventListener("input", () => {
            if (filterTimer) clearTimeout(filterTimer);
            filterTimer = setTimeout(applyAll, 200);
        });

        const cLabel = document.createElement("div");
        cLabel.textContent = "颜色筛选";
        Object.assign(cLabel.style, { fontSize: "13px", fontWeight: "bold", marginBottom: "4px" });
        pop.appendChild(cLabel);

        let cFilter = colorFilter || "none";

        const cdContainer = document.createElement("div");
        Object.assign(cdContainer.style, { position: "relative", width: "100%", marginBottom: "14px" });

        const cdTrigger = document.createElement("div");
        Object.assign(cdTrigger.style, {
            width: "100%", padding: "5px 8px", fontSize: "13px",
            background: "#1a1a1a", border: "1px solid #555", borderRadius: "4px",
            color: "#e0e0e0", boxSizing: "border-box",
            cursor: "pointer", display: "flex", alignItems: "center", gap: "6px", userSelect: "none",
        });
        const cdColorRect = document.createElement("span");
        Object.assign(cdColorRect.style, {
            display: "inline-block", width: "44px", height: "14px",
            borderRadius: "2px", border: "1px solid #555", flexShrink: "0",
        });
        const cdColorText = document.createElement("span");
        Object.assign(cdColorText.style, { flex: "1" });
        const cdArrow = document.createElement("span");
        cdArrow.textContent = "\u25BE";
        Object.assign(cdArrow.style, { marginLeft: "auto", fontSize: "11px", color: "#888" });
        cdTrigger.appendChild(cdColorRect); cdTrigger.appendChild(cdColorText); cdTrigger.appendChild(cdArrow);

        function updateColorPreview() {
            if (cFilter === "none") {
                cdColorRect.style.display = "none"; cdColorText.textContent = "无";
            } else if (cFilter === "__transparent__") {
                cdColorRect.style.display = "inline-block"; cdColorRect.style.background = "transparent";
                cdColorText.textContent = "透明色";
            } else {
                cdColorRect.style.display = "inline-block"; cdColorRect.style.background = cFilter;
                cdColorText.textContent = cFilter.toUpperCase();
            }
        }
        updateColorPreview();
        cdContainer.appendChild(cdTrigger);

        let cdPanel = null;
        function buildColorOptions() {
            const allRaw = rawGroups();
            const colorMap = new Map();
            allRaw.forEach(g => { const c = g.color || "__transparent__"; if (!colorMap.has(c)) colorMap.set(c, g); });
            const colors = Array.from(colorMap.keys());
            if (cdPanel) { cdPanel.remove(); cdPanel = null; }
            cdPanel = document.createElement("div");
            Object.assign(cdPanel.style, {
                position: "absolute", left: "0", right: "0", top: "calc(100% + 2px)",
                background: "#1a1a1a", border: "1px solid #555", borderRadius: "4px",
                zIndex: "100001", maxHeight: "200px", overflowY: "auto",
                boxShadow: "0 4px 16px rgba(0,0,0,0.5)",
            });
            cdPanel.addEventListener("wheel", forwardWheelToCanvas, { passive: false });
            function makeColorItem(bgColor, label, value, isTransparent, isNone) {
                const item = document.createElement("div");
                Object.assign(item.style, {
                    display: "flex", alignItems: "center", gap: "8px",
                    padding: "5px 8px", cursor: "pointer", fontSize: "13px", transition: "background 0.15s",
                });
                item.addEventListener("mouseenter", () => { item.style.background = "#3a3a3a"; });
                item.addEventListener("mouseleave", () => { item.style.background = "transparent"; });
                if (!isNone) {
                    const rect = document.createElement("span");
                    Object.assign(rect.style, {
                        display: "inline-block", width: "44px", height: "16px",
                        borderRadius: "2px", border: "1px solid #555",
                        background: isTransparent ? "transparent" : bgColor, flexShrink: "0",
                    });
                    item.appendChild(rect);
                }
                const txt = document.createElement("span");
                txt.textContent = label; txt.style.color = "#fff"; item.appendChild(txt);
                item.addEventListener("click", (e) => {
                    e.stopPropagation(); cFilter = value; updateColorPreview();
                    if (cdPanel) { cdPanel.remove(); cdPanel = null; } applyAll();
                });
                return item;
            }
            cdPanel.appendChild(makeColorItem("#2a2a2a", "无", "none", false, true));
            colors.forEach(c => {
                if (c === "__transparent__") { cdPanel.appendChild(makeColorItem("#2A2A2A", "透明色", "__transparent__", true, false)); }
                else { cdPanel.appendChild(makeColorItem(c, c.toUpperCase(), c, false, false)); }
            });
            cdContainer.appendChild(cdPanel);
        }
        cdTrigger.addEventListener("click", (e) => {
            e.stopPropagation();
            if (cdPanel) { cdPanel.remove(); cdPanel = null; } else { buildColorOptions(); }
        });
        pop.appendChild(cdContainer);

        const closeColorPanel = (e) => {
            if (cdPanel && !cdContainer.contains(e.target)) { cdPanel.remove(); cdPanel = null; }
        };
        document.addEventListener("mousedown", closeColorPanel);

        const sLabel = document.createElement("div");
        sLabel.textContent = "排序";
        Object.assign(sLabel.style, { fontSize: "13px", fontWeight: "bold", marginBottom: "4px" });
        pop.appendChild(sLabel);

        const sSelect = document.createElement("select");
        Object.assign(sSelect.style, {
            width: "100%", padding: "5px 8px", fontSize: "13px",
            background: "#1a1a1a", border: "1px solid #555", borderRadius: "4px",
            color: "#e0e0e0", outline: "none", boxSizing: "border-box", marginBottom: "14px",
        });
        [["position","按位置"],["alphabet","按首字母"]].forEach(([val, txt]) => {
            const opt = document.createElement("option"); opt.value = val; opt.textContent = txt; sSelect.appendChild(opt);
        });
        sSelect.value = sortOrder; pop.appendChild(sSelect);
        sSelect.addEventListener("change", applyAll);

        const scaleLabel = document.createElement("div");
        scaleLabel.textContent = "UI组件大小";
        Object.assign(scaleLabel.style, { fontSize: "13px", fontWeight: "bold", marginBottom: "4px" });
        pop.appendChild(scaleLabel);

        const scaleRow = document.createElement("div");
        Object.assign(scaleRow.style, { display: "flex", alignItems: "center", gap: "10px", marginBottom: "14px" });

        const scaleInput = document.createElement("input");
        scaleInput.type = "range";
        scaleInput.min = "0.5";
        scaleInput.max = "5.0";
        scaleInput.step = "0.1";
        scaleInput.value = String(uiScale);
        Object.assign(scaleInput.style, {
            flex: "1", height: "4px", appearance: "none", background: "#555",
            borderRadius: "2px", outline: "none", cursor: "pointer",
        });
        const scaleVal = document.createElement("span");
        scaleVal.textContent = uiScale.toFixed(1) + "x";
        Object.assign(scaleVal.style, { minWidth: "36px", fontSize: "12px", color: "#aaa", textAlign: "right" });
        scaleRow.appendChild(scaleInput);
        scaleRow.appendChild(scaleVal);
        pop.appendChild(scaleRow);

        scaleInput.addEventListener("input", () => {
            scaleVal.textContent = (parseFloat(scaleInput.value) || 1.0).toFixed(1) + "x";
            applyAll();
        });

        const tcLabel = document.createElement("div");
        tcLabel.textContent = "主题颜色";
        Object.assign(tcLabel.style, { fontSize: "13px", fontWeight: "bold", marginBottom: "4px" });
        pop.appendChild(tcLabel);

        const colorRow = document.createElement("div");
        Object.assign(colorRow.style, { display: "flex", alignItems: "center", gap: "8px", marginBottom: "16px" });

        const cInput = document.createElement("input");
        cInput.type = "color"; cInput.value = nameColor || "#a89961";
        Object.assign(cInput.style, {
            width: "36px", height: "28px", padding: "0",
            border: "1px solid #555", borderRadius: "4px", background: "#1a1a1a", cursor: "pointer",
        });
        colorRow.appendChild(cInput);

        const cHex = document.createElement("input");
        cHex.type = "text"; cHex.value = nameColor || "#a89961";
        Object.assign(cHex.style, {
            flex: "1", padding: "5px 8px", fontSize: "13px",
            background: "#1a1a1a", border: "1px solid #555", borderRadius: "4px",
            color: "#e0e0e0", outline: "none", boxSizing: "border-box",
        });
        colorRow.appendChild(cHex);

        cInput.addEventListener("input", () => { cHex.value = cInput.value; applyAll(); });
        cHex.addEventListener("input", () => {
            if (/^#[0-9a-fA-F]{6}$/.test(cHex.value)) { cInput.value = cHex.value; applyAll(); }
        });
        pop.appendChild(colorRow);

        document.body.appendChild(pop);
        fInput.focus();
        pop.addEventListener("contextmenu", (e) => { e.preventDefault(); e.stopPropagation(); });
    }

    node._ttGsShowSettings = showSettings;
    node._ttGsSync        = _ttGsSync;


    if (app.graph && typeof app.graph.change === "function") {
        const origGraphChange = app.graph.change;
        app.graph.change = function () {
            if (!selfChanging) dirty = true;
            return origGraphChange.apply(this, arguments);
        };
    }

    function onKeyDown(e) {
        if ((e.ctrlKey || e.metaKey) &&
            (e.key === "m" || e.key === "b" || e.key === "M" || e.key === "B")) {
            setTimeout(() => { dirty = true; }, 100);
        }
    }
    document.addEventListener("keydown", onKeyDown);


    let pageVisible = !document.hidden;
    function onVisibilityChange() {
        const nowVisible = !document.hidden;
        if (nowVisible && !pageVisible) { pageVisible = true; dirty = true; }
        else { pageVisible = nowVisible; }
    }
    document.addEventListener("visibilitychange", onVisibilityChange);


    const timer = setInterval(() => {
        if (!node.graph) {
            clearInterval(timer);
            document.removeEventListener("visibilitychange", onVisibilityChange);
            document.removeEventListener("keydown", onKeyDown);
            const s = document.getElementById(_styleId);
            if (s) s.remove();
            return;
        }

        if (!app.graph || !app.graph._nodes || app.graph._nodes.indexOf(node) < 0) return;
        if (!pageVisible) return;

        if (dirty) {
            dirty = false;
            const cachedAll = rawGroups(true);
            const list = visible(cachedAll);
            const sig = list.map(g => g.title).join("\x00");
            const listChanged = (sig !== lastSig);
            const stateSig = computeStateSig(list, cachedAll);
            const stateChanged = (stateSig !== lastStateSig);

            if (listChanged || stateChanged) {
                lastStateSig = stateSig;
                selfChanging = true;
                try {
                    const syncChanged = syncExternalState(cachedAll);
                    updateDynamicStyles();
                    if (listChanged || syncChanged) {
                        refresh(true);
                    } else {
                        buildDom(true);
                        fitNodeSize();
                    }
                    try { app.graph.change(); } catch (_) {}
                } finally {
                    selfChanging = false;
                }
            }
        }
    }, 500);


    updateDynamicStyles();
    refresh(false);
    lastStateSig = computeStateSig(visible());
    dirty = false;

    requestAnimationFrame(() => { rootEl.style.visibility = "visible"; });
}
