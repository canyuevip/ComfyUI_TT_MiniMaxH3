import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// 音频分段串联 · 一键生成
//
// 分段计划来自「分段JSON」：直接在框里粘贴 JSON（外部语音识别、手工分段都行），
// 或右键把它转换成输入接上游音频分段节点。粘贴时面板立刻解析出每段起止与文字，
// 接上游时由本段执行回传的 ui.tt_audio_chain / status 接口补上。
//
// 「▶ 开始生成」：提交整张图。每段执行时后端的 auto_chain 会认领本段，跑完自动
// 入队下一段；浏览器刷新、关标签页、切工作流都不影响。面板通过 websocket 事件 +
// status 接口恢复/同步显示。
//
// 「停止连跑」= POST /tt/audio_chain/reset：后端把该链的代数 +1 并删掉队列里
// 带链标记的排队项；正在跑的当前段会跑完，后台接力线程随后自行退出。

const CLASS = "TTAudioChainConsole";
const LINE = "TTChainPromptLine";
const LOAD = "TTH3MotionContextLoadLatent";
const SAVE = "TTH3MotionContextSaveLatent";
const MC = "TTH3MotionContext";
const TRIM = "TTH3MotionContextTrim";
const EVENT = "tt_audio_chain_event";

const CSS = [
  ".h3ac{display:flex;flex-direction:column;gap:6px;color:#e8e8e8;font:12px/1.45 system-ui,sans-serif;padding:6px 2px 2px;box-sizing:border-box;width:100%;}",
  ".h3ac-title{font-weight:700;font-size:13px;letter-spacing:.4px;}",
  ".h3ac-meta{opacity:.85;}",
  ".h3ac-bar{height:9px;border-radius:5px;background:#26262b;overflow:hidden;}",
  ".h3ac-fill{height:100%;width:0;border-radius:5px;background:linear-gradient(90deg,#4f8cff,#63d6a6);transition:width .25s;}",
  ".h3ac-next{opacity:.72;font-size:11px;min-height:16px;overflow:hidden;}",
  ".h3ac-text{display:none;font-size:11px;line-height:1.45;max-height:66px;overflow:auto;opacity:.9;border-left:2px solid #4a7ac0;padding-left:6px;white-space:pre-wrap;}",
  ".h3ac-text.show{display:block;}",
  ".h3ac-msg{display:none;font-size:11px;line-height:1.5;border-radius:5px;padding:6px 7px;white-space:pre-wrap;}",
  ".h3ac-msg.show{display:block;}",
  ".h3ac-msg.error{background:#3a1f1f;border:1px solid #a8453f;color:#ffb8b2;}",
  ".h3ac-msg.warn{background:#3a3320;border:1px solid #a8863f;color:#ffe3a3;}",
  ".h3ac-msg.info{background:#20262e;border:1px solid #4a7ac0;color:#cfe0f5;}",
  ".h3ac-row{display:flex;gap:6px;}",
  ".h3ac button{flex:1;cursor:pointer;border:1px solid #4a4a52;background:#2b2b31;color:#e8e8e8;border-radius:6px;padding:7px 6px;font:12px system-ui,sans-serif;}",
  ".h3ac button:hover{background:#3a3a44;}",
  ".h3ac button:disabled{opacity:.4;cursor:default;}",
  ".h3ac button.main{background:#2c4a7a;border-color:#4a7ac0;font-weight:700;}",
  ".h3ac button.main:hover{background:#35598f;}",
  ".h3ac button.stop{background:#7a2c2c;border-color:#c04a4a;font-weight:700;}",
  ".h3ac-check{display:flex;align-items:center;gap:5px;opacity:.7;font-size:11px;",
].join("");

let cssOnce = false;
function injectCss() {
  if (cssOnce) return;
  cssOnce = true;
  const s = document.createElement("style");
  s.textContent = CSS;
  document.head.appendChild(s);
}

function graphNodes() { return app.graph?._nodes || []; }
function classOf(n) { return n?.comfyClass || n?.type; }
function widgetOf(node, name) { return node?.widgets?.find((w) => w.name === name); }

function findPair() {
  const list = graphNodes();
  return {
    load: list.find((n) => classOf(n) === LOAD) || null,
    save: list.find((n) => classOf(n) === SAVE) || null,
  };
}

function numberWidget(node, name) {
  const w = widgetOf(node, name);
  const v = w ? parseFloat(w.value) : NaN;
  return Number.isFinite(v) ? v : 0;
}

// 与服务端 auto_chain.sanitize_prefix 保持一致的净化，用作链 key。
function sanitizePrefix(raw) {
  return String(raw || "").trim().replace(/[<>:"/\\|?*]/g, "_");
}
function prefixOf(node) {
  const w = widgetOf(node, "文件名前缀");
  return sanitizePrefix(w ? w.value : "");
}

// 「分段JSON」已经转换成输入接了上游：以本节点执行后回传的计划为准，框里的内容无效。
function jsonConnected(node) {
  const inp = (node.inputs || []).find((i) => i.name === "分段JSON");
  return !!(inp && inp.link != null);
}

function jsonText(node) {
  const w = widgetOf(node, "分段JSON");
  return w ? String(w.value || "").trim() : "";
}

// 解析「分段JSON」：{"total":…, "segments":[{start_time, duration, text/prompt}]}，
// segments 也可以是裸数组。分不出有效分段时返回 null。
function planFromJson(raw) {
  const text = String(raw || "").trim();
  if (!text) return null;
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    return null;
  }
  const items = Array.isArray(data) ? data : (data && Array.isArray(data.segments) ? data.segments : null);
  if (!items) return null;
  const segments = [];
  for (const s of items) {
    if (!s || typeof s !== "object") continue;
    const duration = parseFloat(s.duration) || 0;
    if (duration <= 0) continue;
    segments.push({
      start: parseFloat(s.start_time != null ? s.start_time : s.start) || 0,
      duration: duration,
      text: String(s.text || s.prompt || ""),
    });
  }
  if (!segments.length) return null;
  const fullText = String((data && (data.text || data.full_text)) || "")
    || segments.map((s) => s.text).filter(Boolean).join(" ");
  return { segments: segments, text: fullText };
}

// 把框里的 JSON 落到面板上；接了上游时保持服务端回传的计划不动。
function refreshPlan(node) {
  const ui = node && node._h3ac;
  if (!ui || jsonConnected(node)) return;
  const plan = planFromJson(jsonText(node));
  ui.segments = plan ? plan.segments : null;
  ui.fullText = plan ? plan.text : "";
}

// 把服务端分段计划套上上下文重叠补偿（第 2 段起 start 前移 overlap、duration 加长）。
function offsetSegments(segs, overlapFrames, fps) {
  return (segs || []).map((s, i) => {
    const start = parseFloat(s.start) || 0;
    const dur = parseFloat(s.duration) || 0;
    let overlap = 0;
    if (i >= 1 && overlapFrames > 0 && fps > 0) overlap = Math.min(overlapFrames / fps, start);
    return { start: start - overlap, duration: dur + overlap, delivered: dur, overlap: overlap, text: s.text || "" };
  });
}

function readState(node) {
  const overlapFrames = numberWidget(node, "上下文帧数");
  const fps = numberWidget(node, "帧率") || 24;
  const cw = widgetOf(node, "当前段");
  const raw = cw ? parseInt(cw.value, 10) : 1;
  const cur = Math.max(1, Number.isFinite(raw) ? raw : 1);

  // 分段计划：框里粘贴的 JSON 由 refreshPlan 现算；接上游时等服务端回传。
  const out = offsetSegments(node._h3ac && node._h3ac.segments, overlapFrames, fps);
  const n = out.length;
  const idx = Math.min(cur, n > 0 ? n : 1) - 1;
  const last = n > 0 ? out[n - 1] : null;
  return {
    total: n, cur: cur, current: n > 0 ? out[idx] : null,
    prev: idx >= 1 ? out[idx - 1] : null,
    whole: last ? last.start + last.duration : 0,
  };
}

// 参数体检：错误会挡住运行，警告只提醒
function validate(node) {
  const out = [];
  const prefix = prefixOf(node);
  if (!prefix) {
    out.push({ level: "warn", text: "⚠ 未填「文件名前缀」：分段没有统一的保存目录，画布上的合并节点会收集不到分段。" });
  }

  if (jsonConnected(node)) {
    if (!(node._h3ac && node._h3ac.segments)) {
      out.push({ level: "warn", text: "⚠ 「分段JSON」接的是上游：跑完第一段后这里才会显示分段计划。想先看分段，可以把它的输出复制成 JSON 粘到输入框里。" });
    }
  } else {
    const raw = jsonText(node);
    if (!raw) {
      out.push({ level: "error", text: "✘ 「分段JSON」是空的：把分段 JSON 粘进输入框，或右键「分段JSON」→ 转换为输入，接上游音频分段节点。" });
    } else if (!planFromJson(raw)) {
      out.push({ level: "error", text: "✘ 「分段JSON」无法解析：需要 {\"total\":126.9,\"segments\":[{\"start_time\":0,\"duration\":10.1,\"text\":\"…\"}]}，segments 也可以是裸数组，每段的文字用 text 或 prompt。" });
    }
  }

  const ovf = numberWidget(node, "上下文帧数");
  if (ovf > 0) {
    if (!graphNodes().some((n) => classOf(n) === TRIM)) {
      out.push({ level: "warn", text: "⚠ 没找到「✂H3运动上下文裁剪」节点，第 2 段起多截的 " + ovf + " 帧不会被剪掉，每段会多出约 " + (ovf / 24).toFixed(2) + " 秒。" });
    }
    const mc = graphNodes().find((n) => classOf(n) === MC);
    const cw = mc ? widgetOf(mc, "context_length") : null;
    const cval = cw ? parseInt(cw.value, 10) : NaN;
    if (Number.isFinite(cval) && cval !== ovf) {
      out.push({ level: "warn", text: "⚠ 运动上下文节点用的是 " + cval + " 帧，这里的「上下文帧数」是 " + ovf + " 帧，两边不一致会让每段时长算错。" });
    }
  }
  return out;
}

function setCurrent(node, value) {
  const st = readState(node);
  const cap = st.total > 0 ? st.total : 99999;
  const v = Math.max(1, Math.min(value, cap));
  const w = widgetOf(node, "当前段");
  if (w) w.value = v;
  return v;
}

// 把“第几段”翻译成 H3 Motion Context 需要的 clip_index：
//   第 1 段 -> Load 0 / Save 1；第 N 段 -> Load N-1 / Save N
function syncGraph(node) {
  const st = readState(node);
  const pair = findPair();
  if (pair.load) {
    const w = widgetOf(pair.load, "clip_index");
    if (w) w.value = Math.max(0, st.cur - 1);
    // 被加载段交付内容在文件里结束的位置：Load 侧据此裁掉渲染多出来的尾巴
    // （衔接头 + 17m+5 对齐），否则结尾漂移出的画面（推近、回坐）会被钉进
    // 本段开头。第 2 段起的段，交付内容前还有被 ✂Trim 剪掉的衔接头
    // （overlap），必须加上它，否则钉的窗口会整体早 22 帧，每个接缝都要
    // 重演上一段约 1 秒画面。自动连跑的接力段由服务端自己注入，这里管的
    // 是手动提交的头一段。
    const tw = widgetOf(pair.load, "target_seconds");
    if (tw) tw.value = st.prev ? st.prev.delivered + st.prev.overlap : 0;
    const fw = widgetOf(pair.load, "fps");
    if (fw) fw.value = numberWidget(node, "帧率") || 24;
  }
  if (pair.save) {
    const w = widgetOf(pair.save, "clip_index");
    if (w) w.value = Math.max(1, st.cur);
  }
  for (const n of graphNodes()) {
    if (classOf(n) !== LINE) continue;
    const w = widgetOf(n, "start_index");
    if (w) w.value = Math.max(0, st.cur - 1);
  }
}

async function clearSlots() {
  const pair = findPair();
  let path = "h3_context";
  if (pair.load) {
    const w = widgetOf(pair.load, "latent_path");
    if (w && w.value) path = w.value;
  }
  try {
    const r = await api.fetchApi("/tt/motion_context/clear_latents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ latent_path: path }),
    });
    if (!r.ok) return 0;
    const j = await r.json();
    return j.removed | 0;
  } catch (e) {
    return 0;
  }
}

// 停止 / 重开同一条链：后端代数 +1 并删掉队列里带链标记的排队项。
async function resetChain(node) {
  try {
    await api.fetchApi("/tt/audio_chain/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prefix: prefixOf(node) }),
    });
  } catch (e) { /* 提交失败会在队列响应里再报，这里不打断 */ }
}

// 提交当前段。面板按钮语义固定：开始/从头=连跑，只跑这一段=单段。
// 「自动连跑」widget 只影响用户直接点队列「运行」的提交，这里临时改完再还原。
async function submit(node, chain) {
  syncGraph(node);
  const w = widgetOf(node, "自动连跑");
  const wasOn = !!(w && w.value);
  if (w) w.value = !!chain;
  try {
    await app.queuePrompt(0, 1);
  } finally {
    if (w) w.value = wasOn;
  }
}

// 问一次服务端链状态，恢复/校正面板显示（页面刷新、多窗口同步也靠它）。
async function refreshStatus(node) {
  const ui = node && node._h3ac;
  if (!ui) return;
  try {
    const r = await api.fetchApi("/tt/audio_chain/status?prefix=" + encodeURIComponent(prefixOf(node)));
    if (!r.ok) return;
    const j = await r.json();
    // 只有「分段JSON」接了上游时才采用服务端回传的计划；框里粘贴的 JSON 面板自己解析。
    if (jsonConnected(node)) {
      const plan = j.plan && j.plan.length ? j.plan : null;
      const planSig = plan ? JSON.stringify(plan) : "";
      if (planSig !== ui.planSig) {
        ui.planSig = planSig;
        ui.segments = plan;
        if (plan) ui.fullText = j.full_text || "";
        paint(node);
      }
    }
    const c = j.chain;
    if (!c) return;
    if (c.running) {
      ui.running = true;
      ui.lastMessage = "";
      if (c.segment) setCurrent(node, c.segment);
    } else {
      ui.running = false;
      if (c.message) ui.lastMessage = c.message;
    }
    paint(node);
  } catch (e) { /* 后端不可达时保持当前显示 */ }
}

// 面板真实内容高度：累加每个可见子元素的高度（含 .h3ac 的 6px 间距与上下内边距）。
// 不去读面板自身的 offsetHeight/scrollHeight——DOM widget 的容器会按我们上报的高度
// 反过来设置元素高度，读自身高度会锁死在首次的猜测值上（消息框消失后也缩不回去）。
function panelHeight(root) {
  let h = 0, rows = 0;
  for (const child of root.children) {
    if (getComputedStyle(child).display === "none") continue;
    h += child.offsetHeight;
    rows++;
  }
  if (rows > 1) h += (rows - 1) * 6;
  return Math.ceil(h + 8);
}

// 按真实内容把节点撑高。把面板高度通过 DOM widget 的 computeSize 报给 LiteGraph，
// 它的 node.computeSize 会连同标题、其余控件与间距一并算好，所以直接用该高度即可；
// 不再手工累加，避免漏算十几个控件的间距导致底部按钮露到框外。
// _fitting 截断 setSize -> 重排 -> ResizeObserver -> 再 setSize 的回环。
function fitToContent(node) {
  const ui = node && node._h3ac;
  if (!ui || !ui.root || ui._fitting) return;
  if (panelHeight(ui.root) <= 8) return;   // 面板还没上屏/量不到内容，等下一帧再算
  ui._fitting = true;
  try {
    const width = Math.max(node.size ? node.size[0] : 0, 380);
    const need = Math.ceil(node.computeSize([width, 0])[1]);
    if (Math.abs(need - (node.size ? node.size[1] : 0)) > 2) node.setSize([width, need]);
  } finally {
    ui._fitting = false;
  }
}

function paint(node) {
  const ui = node && node._h3ac;
  if (!ui) return;
  refreshPlan(node);
  const st = readState(node);
  const issues = validate(node);
  const error = issues.filter((x) => x.level === "error")[0];
  const warns = issues.filter((x) => x.level === "warn");
  const infos = issues.filter((x) => x.level === "info");
  if (error) {
    ui.msg.className = "h3ac-msg show error";
    ui.msg.textContent = error.text;
  } else if (warns.length) {
    ui.msg.className = "h3ac-msg show warn";
    ui.msg.textContent = warns.map((x) => x.text).join("\n");
  } else if (infos.length) {
    ui.msg.className = "h3ac-msg show info";
    ui.msg.textContent = infos.map((x) => x.text).join("\n");
  } else {
    ui.msg.className = "h3ac-msg";
    ui.msg.textContent = "";
  }

  if (st.total === 0) {
    ui.meta.textContent = "还没有分段计划";
    ui.next.textContent = "在「分段JSON」框里粘贴分段 JSON（或把它转换成输入接上游音频分段节点），这里会立刻列出每段起止与文字，然后「▶ 开始生成」逐段生成。";
    ui.fill.style.width = "0%";
  } else {
    const shown = Math.min(st.cur, st.total);
    const seg = st.current;
    ui.meta.textContent = "第 " + shown + " / " + st.total + " 段 · 本段起 " + seg.start.toFixed(1) +
      " 秒 · 时长 " + seg.duration.toFixed(1) + " 秒 · 全片 " + st.whole.toFixed(1) + " 秒";
    const done = Math.max(0, Math.min(st.cur - 1, st.total));
    ui.fill.style.width = ((done / st.total) * 100).toFixed(1) + "%";
    const tail = st.cur >= 2 ? "（已追加无缝衔接说明）" : "";
    if (ui.running) {
      ui.next.textContent = "服务端连跑中 · 已完成 " + done + " 段，还剩 " + (st.total - done) +
        " 段。刷新或关闭页面都不影响，回来后自动接上显示。";
    } else if (ui.lastMessage) {
      ui.next.textContent = ui.lastMessage;
    } else if (st.cur <= st.total) {
      const ov = seg.overlap > 0 ? "（含 " + seg.overlap.toFixed(2) + " 秒衔接，开头会被剪掉）" : "";
      ui.next.textContent = "本段将截取音频 " + seg.start.toFixed(1) + "–" + (seg.start + seg.duration).toFixed(1) +
        " 秒" + ov + tail;
    } else {
      ui.next.textContent = "已跑完 " + st.total + " 段，点「从头开始」可重跑";
    }
  }

  const curSeg = st.total > 0 ? st.current : null;
  if (curSeg && curSeg.text) {
    ui.text.className = "h3ac-text show";
    ui.text.textContent = "本段文字：" + curSeg.text;
  } else if (ui.fullText) {
    ui.text.className = "h3ac-text show";
    ui.text.textContent = "全文：" + ui.fullText;
  } else {
    ui.text.className = "h3ac-text";
    ui.text.textContent = "";
  }

  ui.main.textContent = ui.running ? "⏹ 停止连跑" : (st.total > 0 && st.cur > st.total ? "▶ 重新开始" : "▶ 开始生成");
  ui.main.classList.toggle("main", !ui.running);
  ui.main.classList.toggle("stop", !!ui.running);
  ui.once.disabled = !!ui.running;
  fitToContent(node);
}

function flash(node, text) {
  const ui = node && node._h3ac;
  if (!ui) return;
  ui.next.textContent = text;
  clearTimeout(ui.timer);
  ui.timer = setTimeout(() => paint(node), 3000);
}

// 服务端链事件：queued 推进段号，其余类型落到最近消息里。
function applyChainEvent(d) {
  if (!d || !d.key) return;
  for (const n of graphNodes()) {
    if (classOf(n) !== CLASS || !n._h3ac) continue;
    if (prefixOf(n) !== d.key) continue;
    const ui = n._h3ac;
    if (d.type === "queued") {
      ui.running = true;
      ui.lastMessage = "";
      if (d.segment) setCurrent(n, d.segment);
    } else {
      ui.running = false;
      if (d.message) ui.lastMessage = d.message;
    }
    paint(n);
  }
}

function buildPanel(node) {
  const root = document.createElement("div");
  root.className = "h3ac";
  root.innerHTML = [
    '<div class="h3ac-title">音频分段串联 · 一键生成</div>',
    '<div class="h3ac-meta"></div>',
    '<div class="h3ac-bar"><div class="h3ac-fill"></div></div>',
    '<div class="h3ac-next"></div>',
    '<div class="h3ac-text"></div>',
    '<div class="h3ac-msg"></div>',
    '<div class="h3ac-row"><button class="main">▶ 开始生成</button></div>',
    '<div class="h3ac-row"><button data-act="once">⏭ 只跑这一段</button><button data-act="restart">⏮ 从头开始</button></div>',
    '<label class="h3ac-check"><input type="checkbox" checked> 从头开始时清除旧记录</label>',
  ].join("");
  for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click", "dblclick", "contextmenu", "wheel"]) {
    root.addEventListener(type, (e) => e.stopPropagation());
  }
  const main = root.querySelector("button.main");
  const once = root.querySelector('button[data-act="once"]');
  const restart = root.querySelector('button[data-act="restart"]');
  const check = root.querySelector('input[type="checkbox"]');
  main.title = "从当前段开始，服务端一段接一段跑到最后。刷新页面、关闭浏览器都不中断。运行中会变成「停止连跑」。";
  once.title = "只生成当前这一段，不推进段号、不连跑。用来调参、试效果，或者重跑某一段。";
  restart.title = "回到第 1 段并立刻开始连跑。勾选了下面的选项时，先清除 h3_context 里保存的衔接记录再从头跑。";
  check.title = "勾选后，点「从头开始」会删除 h3_context 里编号的 clip_0000X.safetensors，避免从旧链的中间段续跑。";
  node._h3ac = {
    root: root,
    meta: root.querySelector(".h3ac-meta"),
    fill: root.querySelector(".h3ac-fill"),
    next: root.querySelector(".h3ac-next"),
    text: root.querySelector(".h3ac-text"),
    msg: root.querySelector(".h3ac-msg"),
    main: main,
    once: once,
    restart: restart,
    timer: 0,
    running: false,
    lastMessage: "",
    watch: 0,
    segments: null,   // 当前分段计划（含每段文字），来自「分段JSON」框或服务端回传
    fullText: "",     // 整段识别文字
    planSig: "",
  };
  main.onclick = async (e) => {
    e.stopPropagation();
    const ui = node._h3ac;
    if (ui.running) {
      ui.running = false;
      ui.lastMessage = "";
      paint(node);
      await resetChain(node);
      return;
    }
    const err = validate(node).filter((x) => x.level === "error")[0];
    if (err) { paint(node); return; }
    const st = readState(node);
    if (st.cur > st.total) setCurrent(node, 1);
    await resetChain(node);   // 清掉可能残留的旧链，保证同前缀只有一条
    ui.running = true;
    ui.lastMessage = "";
    paint(node);
    try {
      await submit(node, true);
    } catch (err2) {
      ui.running = false;
      flash(node, "提交失败：" + (err2 && err2.message ? err2.message : err2));
    }
  };
  once.onclick = async (e) => {
    e.stopPropagation();
    const err = validate(node).filter((x) => x.level === "error")[0];
    if (err) { paint(node); return; }
    await resetChain(node);   // 「只跑这一段」不打连跑
    try {
      await submit(node, false);
    } catch (err2) {
      flash(node, "提交失败：" + (err2 && err2.message ? err2.message : err2));
    }
  };
  restart.onclick = async (e) => {
    e.stopPropagation();
    const err = validate(node).filter((x) => x.level === "error")[0];
    if (err) { paint(node); return; }
    await resetChain(node);
    if (check.checked) await clearSlots();
    setCurrent(node, 1);
    node._h3ac.running = true;
    node._h3ac.lastMessage = "";
    paint(node);
    try {
      await submit(node, true);
    } catch (err2) {
      node._h3ac.running = false;
      flash(node, "提交失败：" + (err2 && err2.message ? err2.message : err2));
    }
  };
  return root;
}

api.addEventListener(EVENT, (e) => applyChainEvent(e.detail || e.data || {}));

// 「分段JSON」接了上游时，本节点执行后会把实际使用的分段计划放在 ui.tt_audio_chain
// 里发回，面板据此显示每段起止与文字。框里粘贴 JSON 时本地已解析，无需等回传。
api.addEventListener("executed", (e) => {
  const d = e.detail || e.data || {};
  const plan = d.output && d.output.tt_audio_chain;
  if (!plan || !plan.segments) return;
  for (const n of graphNodes()) {
    if (classOf(n) !== CLASS || !n._h3ac || !jsonConnected(n)) continue;
    if (String(n.id) !== String(d.node)) continue;
    const ui = n._h3ac;
    ui.segments = plan.segments;
    ui.fullText = plan.full_text || "";
    paint(n);
  }
});

app.registerExtension({
  name: "tt.audio_chain_console",
  setup() {
    const orig = app.queuePrompt ? app.queuePrompt.bind(app) : null;
    if (typeof orig !== "function") return;
    app.queuePrompt = async function () {
      try {
        for (const n of graphNodes()) if (classOf(n) === CLASS) syncGraph(n);
      } catch (err) {
        console.warn("[Audio Chain Console] sync failed", err);
      }
      return orig.apply(null, arguments);
    };
  },
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== CLASS) return;
    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onCreated ? onCreated.apply(this, arguments) : undefined;
      injectCss();
      const root = buildPanel(this);
      const dom = this.addDOMWidget("h3ac", "H3AC", root, {
        serialize: false,
        getHeight: () => panelHeight(root),
        getMinHeight: () => panelHeight(root),
      });
      // 让 LiteGraph 用面板真实高度给这个 widget 留位；否则它按默认高度记账，
      // node.computeSize 偏矮，底部两行按钮与勾选框就会画到节点框外。
      dom.computeSize = (width) => [width || (this.size ? this.size[0] : 380), panelHeight(root)];
      this._h3ac.dom = dom;
      const fit = () => fitToContent(this);
      fit();
      requestAnimationFrame(fit);
      setTimeout(fit, 80);
      setTimeout(fit, 400);
      try {
        const ro = new ResizeObserver(() => fit());
        ro.observe(root);
        this._h3ac.ro = ro;
      } catch (e) { /* 老前端没有 ResizeObserver，靠上面的定时器兜底 */ }
      for (const name of ["公共提示词", "当前段", "上下文帧数", "帧率", "文件名前缀", "识别文字并入提示词", "自动连跑", "每轮段数", "分段JSON"]) {
        const wd = widgetOf(this, name);
        if (!wd) continue;
        const prev = wd.callback;
        wd.callback = (...args) => {
          const out = prev ? prev.apply(wd, args) : undefined;
          paint(this);
          return out;
        };
      }
      paint(this);
      let lastSig = "";
      const timer = setInterval(() => {
        if (!graphNodes().includes(this)) {
          clearInterval(timer);
          if (this._h3ac.watch) clearInterval(this._h3ac.watch);
          try { if (this._h3ac.ro) this._h3ac.ro.disconnect(); } catch (e) {}
          return;
        }
        fitToContent(this);
        const sig = numberWidget(this, "上下文帧数") + "|" + numberWidget(this, "帧率") + "|" +
          (widgetOf(this, "当前段") ? widgetOf(this, "当前段").value : "") + "|" + jsonText(this);
        if (sig !== lastSig) { lastSig = sig; paint(this); }
      }, 800);
      // 与服务端链状态对账：恢复显示、多窗口同步、丢事件兜底，一并解决。
      this._h3ac.watch = setInterval(() => refreshStatus(this), 5000);
      return r;
    };
    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const r = onConfigure ? onConfigure.apply(this, arguments) : undefined;
      paint(this);
      refreshStatus(this);
      return r;
    };
  },
});
