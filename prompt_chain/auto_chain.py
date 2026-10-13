"""音频分段串联的服务端连跑。

🎬音频分段串联·一键生成 每段执行时调用 schedule()：在当前运行的队列项里
认领本段，然后由后台接力线程接管——本段成功后克隆整张图、推进段号与
clip_index、随机化种子、直接入队下一段。合并与保存不在链里做：由画布上的
合并/保存节点凭控制台的「完成信号」在最后一段自行收尾。
每轮段数大于 0 时，从起跑段算起跑满该段数就先停在停点，等手动续跑。
整条链跑在 ComfyUI 进程内，浏览器刷新、关标签页、切工作流都不影响。

停止/开始用「代际」表达：reset 路由把该链的代数 +1 并删光队列里带链标记
的旧项，旧接力线程发现代数对不上就自行退出，天然免疫取消与入队竞态。
"""

import copy
import random
import re
import threading
import time
import uuid

_CONSOLE = "TTAudioChainConsole"
_LOAD = "TTH3MotionContextLoadLatent"
_SAVE = "TTH3MotionContextSaveLatent"
_LINE = "TTChainPromptLine"
_SEED_KEYS = ("seed", "noise_seed")
_MARK = "tt_audio_chain"   # 克隆项 extra_data 里的链标记键
_EVENT = "tt_audio_chain_event"

_lock = threading.Lock()
_gens = {}    # chain_key -> 代数，reset 时 +1
_chains = {}   # chain_key -> 最近状态（status 路由用）
_plans = {}    # chain_key -> {"segments": [{start,duration,text}], "full_text": str}


def sanitize_prefix(raw):
    return re.sub(r'[<>:"/\\|?*]', "_", str(raw or "").strip())


def publish(key, segments, full_text=""):
    """登记当前音频分析分段计划与全文，供前端状态接口读取；segments 为空时清空。"""
    if not key:
        return
    with _lock:
        if segments:
            _plans[key] = {"segments": list(segments), "full_text": str(full_text or "")}
        else:
            _plans.pop(key, None)


def _server():
    try:
        from server import PromptServer
        return PromptServer.instance
    except Exception:
        return None


def _emit(type_, key, segment, total, message):
    server = _server()
    if server is None:
        return
    try:
        server.send_sync(_EVENT, {"type": type_, "key": key, "segment": segment,
                                   "total": total, "message": message})
    except Exception:
        pass


def _num(value):
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _console_matches(node, segment, prefix):
    """链身份只看「当前段」与「文件名前缀」——这两个永远是 widget 字面值。
    「分段JSON」之类的输入可能来自 widget 也可能接了上游，接了上游时 prompt 里存的是
    [node_id, slot] 引用而不是内容，不能拿来比较。"""
    inputs = node.get("inputs", {})
    if _num(inputs.get("当前段")) != float(segment):
        return False
    return sanitize_prefix(inputs.get("文件名前缀")) == prefix


def schedule(segment, count, prefix, per_cycle=0):
    """本段执行期间由 TTAudioChainConsole.resolve 调用：认领当前队列项，
    启动接力线程。认领不到或后端不可用时静默放弃，绝不拖垮本段渲染。"""
    server = _server()
    if server is None:
        return
    try:
        running = server.prompt_queue.get_current_queue_volatile()[0]
    except Exception:
        return
    item = None
    for candidate in running:
        for node in candidate[2].values():
            if (str(node.get("class_type", "")) == _CONSOLE
                    and _console_matches(node, segment, prefix)):
                item = candidate
                break
        if item is not None:
            break
    if item is None:
        return
    # 每轮停点：随链标记一路传下去，整轮只认第一次算出的 cycle_end——逐段重算
    # 会让停点一直往前挪（1 起算 4 段、2 起算又 4 段），永远到不了头。
    extra = item[3] if isinstance(item[3], dict) else {}
    mark = extra.get(_MARK)
    if isinstance(mark, dict) and isinstance(mark.get("cycle_end"), int):
        cycle_end = min(int(mark["cycle_end"]), int(count))
    elif int(per_cycle) > 0:
        cycle_end = min(int(count), int(segment) + int(per_cycle) - 1)
    else:
        cycle_end = int(count)
    with _lock:
        gen = _gens.get(prefix, 0)
        _chains[prefix] = {"gen": gen, "segment": int(segment), "total": int(count),
                           "running": True, "message": "", "updated": time.time()}
    threading.Thread(
        target=_relay, daemon=True, name="tt-audio-chain-%s" % prefix,
        kwargs=dict(item=item, segment=int(segment), count=int(count), key=prefix,
                    gen=gen, cycle_end=cycle_end),
    ).start()


def _relay(item, segment, count, key, gen, cycle_end):
    server = _server()
    if server is None:
        return
    queue = server.prompt_queue
    pid = item[1]
    # 等本段跑完（中断/出错也走 task_done，history 里不是 success 即停链）
    while True:
        with _lock:
            if _gens.get(key, 0) != gen:
                return
        if not any(it[1] == pid for it in queue.get_current_queue_volatile()[0]):
            break
        time.sleep(1.0)
    # task_done 紧随执行结束写入 history，稍等它落账
    status = None
    for _ in range(3):
        status = queue.get_history(pid, map_function=lambda x: x).get(pid, {}).get("status")
        if status is not None:
            break
        time.sleep(1.0)
    if not status or str(status.get("status_str", "")).lower() != "success":
        _finish(key, gen, segment, count, "failed",
                "第 %d 段没有成功完成，连跑已停止" % segment)
        return
    if segment >= count:
        # 最后一段：合并与保存由画布上的节点凭「完成信号」在同一 prompt 里收尾，
        # 链这边只报完成。
        _finish(key, gen, count, count, "done", "🎉 全部 %d 段已完成" % count)
        return
    if segment >= cycle_end:
        # 每轮停点：本轮段数跑满就停；下一轮由用户手动改「当前段」再开始，
        # 中间留给中途检查或人工处理。
        _finish(key, gen, segment, count, "cycle_done",
                "本轮连跑到第 %d 段跑满停点，已停止；续跑请把「当前段」设成 %d 后重新开始。"
                % (segment, segment + 1))
        return
    nxt = segment + 1
    err = _enqueue_next(server, item, nxt, count, key, gen, cycle_end)
    if err == "reset":
        return
    if err:
        _finish(key, gen, segment, count, "failed", err)
        return
    with _lock:
        st = _chains.get(key)
        if st is not None and st.get("gen") == gen:
            st["segment"] = nxt
            st["updated"] = time.time()
    _emit("queued", key, nxt, count, "第 %d / %d 段已入队" % (nxt, count))


def _enqueue_next(server, item, nxt, count, key, gen, cycle_end):
    """克隆当前图并入队第 nxt 段。返回 None 成功，'reset' 表示链已被重置，其余为失败原因。"""
    prompt = copy.deepcopy(item[2])
    hit = False
    # 上一段交付内容在文件里结束的位置（秒）：Load 侧据此裁掉渲染多出来的
    # 尾巴（衔接头 + 17m+5 对齐），否则没人看过的结尾（推近、回坐的漂移）
    # 会被钉进本段开头。第 2 段起的交付内容前还有被 ✂Trim 剪掉的衔接头
    # （那一段生成时控制台前移的时长），漏掉它钉的窗口会整体早 22 帧，
    # 每个接缝都要重演上一段约 1 秒的画面。帧率与上下文帧数要先扫一遍拿到
    # ——字典顺序不保证 Load 在 Console 之后。
    fps = 24.0
    ctx_frames = 0.0
    for node in prompt.values():
        if (str(node.get("class_type", "")) == _CONSOLE
                and _console_matches(node, nxt - 1, key)):
            inputs = node.get("inputs", {})
            fps = _num(inputs.get("帧率")) or 24.0
            ctx_frames = _num(inputs.get("上下文帧数")) or 0.0
            break
    with _lock:
        entry = _plans.get(key)
        plan = list(entry["segments"]) if entry else []
    prev_seconds = plan[nxt - 2]["duration"] if 2 <= nxt <= len(plan) + 1 else None
    if prev_seconds is not None and nxt - 2 >= 1 and ctx_frames > 0:
        prev_seconds += min(ctx_frames / fps, float(plan[nxt - 2]["start"]))
    for node in prompt.values():
        class_type = str(node.get("class_type", ""))
        inputs = node.get("inputs", {})
        if class_type == _CONSOLE:
            if _console_matches(node, nxt - 1, key):
                inputs["当前段"] = nxt
                hit = True
        elif class_type == _LOAD:
            inputs["clip_index"] = nxt - 1
            if prev_seconds:
                inputs["target_seconds"] = float(prev_seconds)
                inputs["fps"] = float(fps)
        elif class_type == _SAVE:
            inputs["clip_index"] = nxt
        elif class_type == _LINE:
            inputs["start_index"] = nxt - 1
        for k in _SEED_KEYS:
            v = inputs.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                inputs[k] = random.getrandbits(32)
    if not hit:
        return "第 %d 段无法入队：没找到 🎬音频分段串联·一键生成 节点" % nxt
    extra = dict(item[3])
    extra["create_time"] = int(time.time() * 1000)
    extra[_MARK] = {"key": key, "gen": gen, "segment": nxt, "total": count,
                    "cycle_end": cycle_end}
    sensitive = dict(item[5]) if item[5] else {}
    outputs = list(item[4]) if item[4] else []
    with _lock:
        if _gens.get(key, 0) != gen:
            return "reset"
        number = server.number
        server.number += 1
        server.prompt_queue.put((number, str(uuid.uuid4()), prompt, extra, outputs, sensitive))
    return None


def _finish(key, gen, segment, count, type_, message):
    with _lock:
        if _gens.get(key, 0) != gen:
            return
        st = _chains.get(key)
        if st is not None:
            st["running"] = False
            st["message"] = message
            st["updated"] = time.time()
    _emit(type_, key, segment, count, message)


def _chain_pred(key):
    def pred(item):
        mark = item[3].get(_MARK)
        return isinstance(mark, dict) and mark.get("key") == key
    return pred


def register_audio_chain_routes():
    """POST /tt/audio_chain/reset 停链（代数+1、删排队项），GET status 查询链状态。"""
    try:
        from aiohttp import web
        from server import PromptServer
    except ImportError:
        return
    server = getattr(PromptServer, "instance", None)
    if server is None or getattr(register_audio_chain_routes, "_done", False):
        return

    from ..motion_ctx.csrf_guard import require_same_origin

    @server.routes.post("/tt/audio_chain/reset")
    @require_same_origin
    async def _reset_route(request):
        data = await request.json()
        key = sanitize_prefix(data.get("prefix"))
        if not key:
            return web.json_response({"error": "缺少 prefix"}, status=400)
        queue = server.prompt_queue
        with _lock:
            _gens[key] = _gens.get(key, 0) + 1
            gen = _gens[key]
            while queue.delete_queue_item(_chain_pred(key)):
                pass
            st = _chains.get(key)
            was_running = bool(st and st.get("running"))
            if st is not None and was_running:
                st["running"] = False
                st["message"] = "已停止连跑（当前段跑完后不再推进）"
                st["updated"] = time.time()
        # 链本来就在跑，这次 reset 是「停止」；没在跑则是开始/重开前的静默重置，不广播
        if was_running:
            _emit("stopped", key, 0, 0, "已停止连跑（当前段跑完后不再推进）")
        return web.json_response({"ok": True, "gen": gen})

    @server.routes.get("/tt/audio_chain/status")
    async def _status_route(request):
        key = sanitize_prefix(request.rel_url.query.get("prefix", ""))
        if not key:
            return web.json_response({"error": "缺少 prefix"}, status=400)
        with _lock:
            chain = dict(_chains[key]) if key in _chains else None
            entry = _plans.get(key)
            plan = list(entry["segments"]) if entry else None
            full_text = entry["full_text"] if entry else None
        return web.json_response({"chain": chain, "plan": plan, "full_text": full_text})

    register_audio_chain_routes._done = True
