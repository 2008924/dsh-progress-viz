#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh 进度可视化 —— 多任务分栏看板服务器（零 dsh-project 依赖，可单独运行）

不依赖 dsh_dispatch.py / running.json：直接监控 ~/.dsh/sessions 下**所有**
cwd 编码目录里的全部会话文件（session.jsonl.zstd），收集为「最近任务列表」：
只保留最近 1 小时内有写入的任务，按文件 mtime 降序取前 8 个；每个任务
独立解析（阶段 / 动作 / ETA / 事件流），/api/live 返回 {"tasks": [...]}。
ETA 为「阶段线性外推 + 同目录历史会话耗时中位数」加权融合（详见
compute_hist_s / blend_eta），历史会话排除任务自身。

用法: python dashboard.py [port]   (默认 8123)
  启动成功后自动用默认浏览器打开 http://127.0.0.1:<实际端口>
  （--no-open 关闭自动打开）；端口被占用时自动 +1 递增（最多 5 次）。

纯本地运行，任何解压/解析失败都静默容忍，绝不崩溃。
"""
import argparse
import json
import os
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import session_progress as sp

HERE = os.path.dirname(os.path.abspath(__file__))

POLL_INTERVAL = 4.0   # 轮询间隔（秒）：重新扫描最近任务列表
TAIL_LINES = 15       # tail 事件流条数（最近 ~15 个事件）
TAIL_CUT = 80         # 单条事件文本截断长度（字符）
RECENT_WINDOW = 3600  # 最近任务窗口（秒）：只展示 1 小时内有写入的任务
MAX_TASKS = 8         # 任务卡片上限（按 mtime 降序取前 8 个）
RUNNING_AGE = 30      # 文件 mtime 距今 ≤ 30s 视为 running，否则 completed

# —— 共享状态：后台轮询线程写入，HTTP 线程只读（_LOCK 保护）——
_LOCK = threading.Lock()
_STATE = {"tasks": []}   # 最近任务列表快照（scan_tasks 的结果）


def scan_session_files():
    """全库扫描 → 所有会话文件的 (路径, mtime)，按 mtime 降序。

    布局：<SESSIONS_ROOT>/<cwd编码>/<session-id>/session.jsonl.zstd
    （兼容个别把 zstd 直接平铺在编码目录下的情况）。
    任何目录扫描失败都静默跳过（返回已收集的部分）。
    """
    root = sp.SESSIONS_ROOT
    if not os.path.isdir(root):
        return []
    found = []
    try:
        cwd_dirs = os.listdir(root)
    except OSError:
        return []
    for d in cwd_dirs:
        p = os.path.join(root, d)
        try:
            if not os.path.isdir(p):
                continue
            for name in os.listdir(p):
                sub = os.path.join(p, name)
                if os.path.isdir(sub):
                    f = os.path.join(sub, "session.jsonl.zstd")
                elif name.endswith(".zstd"):
                    f = sub  # 直接平铺在编码目录下的会话文件
                else:
                    continue
                if os.path.isfile(f):
                    found.append((f, os.path.getmtime(f)))
        except OSError:
            continue  # 单个目录扫描失败静默跳过
    found.sort(key=lambda x: x[1], reverse=True)  # 按 mtime 降序
    return found


def _meta_from_lines(lines):
    """从事件行解析会话元信息（type=session 事件，字段在事件顶层）。

    tag 取会话 id 去掉 "session-" 前缀后的前 8 位（如 27885bb9）；
    task 用事件里的 cwd（比编码目录名更完整）；started_at 用 createdAt(ms)。
    返回 {"tag": ..., "task": ..., "started_at": epoch 秒或 None}。
    """
    for ln in lines:
        try:
            ev = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(ev, dict) and ev.get("type") == "session":
            sid = ev.get("id") or ""
            tag = sid[8:16] if sid.startswith("session-") else (sid[:8] or None)
            created_ms = ev.get("createdAt")
            started = None
            if isinstance(created_ms, (int, float)) and created_ms > 0:
                started = created_ms / 1000.0
            return {"tag": tag, "task": ev.get("cwd"), "started_at": started}
    return {"tag": None, "task": None, "started_at": None}


def _message_text(data):
    """assistant/message 的 data → 关键文本（text 段优先，回退 reasoning）。"""
    msg = data.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    parts = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                parts.append(c["text"])
        if not parts:  # 无 text 段 → 回退 reasoning 草稿
            for c in content:
                if isinstance(c, dict) and c.get("type") == "reasoning" and c.get("text"):
                    parts.append(c["text"])
    elif isinstance(content, str):
        parts.append(content)
    return " ".join(parts).strip()


def format_tail_line(ev, todo_count=0):
    """单个事件 → 紧凑单行文本（事件类型 + 关键内容，截断 TAIL_CUT 字符）。

    例：「tool/call bash: pytest -q」「assistant/message: 正在分析需求」
    「todo: 当前第 2 项/共 3 项」（todo_count = 截至当前已写过的 todo/write 次数，
    无 status 字段的清单用「第(次数-1)项」规则，与 build_progress 一致）。
    headless 模式无 stdout，事件流就是任务的"输出"。
    """
    typ, data = ev
    text = typ
    if typ == "tool/call" and isinstance(data, dict):
        name = data.get("name") or ""
        args = data.get("arguments")
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
                if isinstance(parsed, dict):
                    args = parsed
            except (json.JSONDecodeError, ValueError):
                pass  # arguments 不是 JSON → 按原样处理
        detail = ""
        if name == "bash":
            if isinstance(args, dict):
                detail = args.get("command") or args.get("cmd") or ""
            elif isinstance(args, str):
                detail = args
        elif name in ("read", "write"):
            if isinstance(args, dict) and args.get("file_path"):
                detail = str(args["file_path"])
        else:
            if isinstance(args, dict):
                detail = json.dumps(args, ensure_ascii=False)
            elif args:
                detail = str(args)
        text = f"tool/call {name}: {detail}"  # 突出动作：工具名 + 参数摘要
    elif typ == "assistant/message" and isinstance(data, dict):
        text = f"assistant/message: {_message_text(data)}"
    elif typ == "todo/write" and isinstance(data, dict):
        items = data.get("todos") or data.get("items") or []
        if isinstance(items, list) and items:
            idx, total = sp._pick_todo_stage(items, todo_count)
            text = f"todo: 当前第 {idx} 项/共 {total} 项"
        else:
            text = "todo/write"
    elif typ == "step/start" and isinstance(data, dict):
        text = f"step/start: 步骤{data.get('step', '')}"
    return text[:TAIL_CUT]


# 需要合并的连续同类 chunk 事件类型（占事件流绝大多数且内容无差别）
_CHUNK_TYPES = ("reasoning-chunks", "tool-call-chunks", "assistant-chunks")


def _ts_text(i, times):
    """事件 i 的时间戳文本：[HH:MM:SS]（有 time 字段）或 [序号]（无 time）。

    times 为与 events 平行的 epoch 秒列表；无 time（None/缺省/非法）时用
    文件事件顺序序号（1-based 位置）代替，保证每条 tail 都有时间锚点。
    """
    t = times[i] if times is not None and i < len(times) else None
    if isinstance(t, (int, float)) and t > 0:
        return time.strftime("[%H:%M:%S]", time.localtime(t))
    return "[%d]" % (i + 1)


def format_tail(events, times=None, max_lines=TAIL_LINES):
    """事件流 → 可读性优化后的 tail 行列表（最多 max_lines 条，合并后计数）。

    规则（按 spec）：
      - 连续相同的 reasoning-chunks / tool-call-chunks / assistant-chunks
        合并为一条「type ×N」（N=连续出现次数，N==1 不显示 ×1），不再刷屏；
      - tool/call 突出为「tool/call 工具名: 参数摘要」（复用 format_action 思路）；
      - todo/write 显示「todo: 当前第 k 项/共 n 项」；
      - 每条前加 [HH:MM:SS]（time 字段毫秒时间戳 → 本地时间，times 已换算为秒）；
        无 time 字段用文件事件顺序序号 [N] 代替；
      - 保留最近 max_lines 条（合并后计数）。

    events: parse_event 的 (type, data) 列表；times: 平行的 epoch 秒列表
    （None 表示该事件无 time），与 _read_task 的返回约定一致。
    """
    if max_lines <= 0:
        return []
    merged = []  # 合并后的 (时间戳文本, 行文本) 列表
    todo_count = 0
    i, n = 0, len(events)
    while i < n:
        ev = events[i]
        typ = ev[0] if isinstance(ev, (tuple, list)) and ev else None
        if typ in _CHUNK_TYPES:
            # 扫描连续相同 chunk 的运行长度
            j = i
            while j < n and isinstance(events[j], (tuple, list)) \
                    and events[j][0] == typ:
                j += 1
            count = j - i
            text = f"{typ} ×{count}" if count > 1 else typ
            merged.append((_ts_text(i, times), text))  # 时间戳取运行首事件
            i = j
            continue
        if typ == "todo/write":
            todo_count += 1
        merged.append((_ts_text(i, times), format_tail_line(ev, todo_count)))
        i += 1
    return [ts + " " + text for ts, text in merged[-max_lines:]]


def _read_task(path):
    """读取单个会话文件 → (lines, events, times, started_at)；失败返回全 None。

    复用 sp.tail_session / sp.parse_event；times 为各事件顶层 time（秒），
    session 事件无 time 时用 createdAt 兜底；started_at 取首个有效时间。
    """
    try:
        lines, _ = sp.tail_session(path, 0)
    except Exception:
        return None, None, None, None
    events, times = [], []
    started_at = None
    for ln in lines:
        ev = sp.parse_event(ln)
        if ev is None:
            continue
        events.append(ev)
        t_ms = None
        try:
            obj = json.loads(ln)
            if isinstance(obj, dict):
                t_ms = obj.get("time")
                if not isinstance(t_ms, (int, float)) or t_ms <= 0:
                    # session 事件无 time 时用 createdAt 兜底
                    t_ms = obj.get("createdAt") if obj.get("type") == "session" else None
        except (json.JSONDecodeError, ValueError):
            pass
        t = t_ms / 1000.0 if isinstance(t_ms, (int, float)) and t_ms > 0 else None
        times.append(t)
        if t is not None and started_at is None:
            started_at = t
    if not events:
        return None, None, None, None
    return lines, events, times, started_at


def _stage_marks(events, times, started_at):
    """阶段变化时间戳列表（秒）：起点=started_at，之后每次阶段变化追加。

    与 sp.build_progress 同规则（todo 清单优先 / step/start 兜底），
    线性扫描一次；阶段变化用对应事件的 time（无 time 时用起点近似）。
    供 blend_eta 的线性外推使用（marks[0]=任务起点）。
    """
    marks = [started_at]
    todo_count, todo_items, step_count = 0, None, 0
    cur_idx, cur_total = 0, 0
    for ev, t in zip(events, times):
        typ, data = ev
        changed = False
        if typ == "todo/write" and isinstance(data, dict):
            todo_count += 1
            items = data.get("items") or data.get("todos")
            if isinstance(items, list) and items:
                todo_items = items
                idx, total = sp._pick_todo_stage(items, todo_count)
                if (idx, total) != (cur_idx, cur_total):
                    cur_idx, cur_total = idx, total
                    changed = True
        elif typ == "step/start" and todo_items is None:
            step_count += 1  # 无 todo 时 step/start 计数兜底
            if step_count != cur_idx:
                cur_idx = step_count
                changed = True
        if changed:
            marks.append(t if t is not None else started_at)
    return marks


def _task_eta(path, cwd, started_at, marks, k, n):
    """单个任务的 ETA 融合（复用 blend_eta；历史会话排除任务自身）。

    返回 (eta_s, eta_mode, eta_at)；无可用信息时 eta_s=None。
    """
    state = {"started_at": started_at, "stage_marks": marks,
             "stage_idx": k, "stage_total": n}
    hist_s = compute_hist_s(cwd, path)
    eta_s, eta_mode = blend_eta(state, hist_s)
    eta_at = (time.strftime("%H:%M:%S",
              time.localtime(time.time() + eta_s))
              if eta_s is not None else None)
    return eta_s, eta_mode, eta_at


def _build_task(path, status, now):
    """单个会话文件 → 任务对象 dict；解析失败返回 None（静默跳过，不崩溃）。

    复用现有解析函数：_meta_from_lines（id/cwd）、sp.build_progress（阶段/动作）、
    blend_eta（ETA）、format_tail_line（事件流紧凑文本）。
    """
    try:
        lines, events, times, started_at = _read_task(path)
        if not events or started_at is None:
            return None
        meta = _meta_from_lines(lines)
        cwd = meta["task"]
        prog = sp.build_progress(events)
        k, n = prog.get("stage_idx", 0), prog.get("stage_total", 0)
        marks = _stage_marks(events, times, started_at)
        eta_s, eta_mode, eta_at = _task_eta(path, cwd, started_at, marks, k, n)
        return {
            "id": meta["tag"],
            "cwd": cwd,
            "status": status,
            "stage": prog.get("stage"),
            "stage_idx": k,
            "stage_total": n,
            "stage_pct": round(k / n * 100) if n else 0,
            "action": prog.get("action"),
            "eta_s": eta_s,
            "eta_mode": eta_mode,
            "eta_at": eta_at,
            "elapsed_s": int(now - started_at),
            "tail": format_tail(events, times, TAIL_LINES),
        }
    except Exception:
        return None  # 单个任务解析失败静默跳过


def scan_tasks(now=None):
    """全库扫描 → 最近任务列表（多任务分栏数据源）。

    - 收集全部会话文件，只保留最近 1 小时内有写入（mtime 距今 < 3600s）的；
    - 按 mtime 降序取前 8 个；
    - 每个任务独立解析；status：文件 mtime 距今 ≤ 30s → running，否则 completed；
    - 单个会话扫描/解析失败静默跳过；无任务返回 []。
    """
    if now is None:
        now = time.time()
    tasks = []
    for path, mt in scan_session_files():
        age = now - mt
        if age >= RECENT_WINDOW:
            break  # 已按 mtime 降序，之后只会更旧 → 直接结束
        status = "running" if age <= RUNNING_AGE else "completed"
        task = _build_task(path, status, now)
        if task is not None:
            tasks.append(task)
        if len(tasks) >= MAX_TASKS:
            break
    return tasks


# —— ETA 历史会话缓存：{会话目录: (目录 mtime, hist_s)}，mtime 未变时复用 ——
_HIST_CACHE = {}


def _median(values):
    """中位数：奇数个取中间值，偶数个取中间两值平均；空列表返回 0。"""
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0
    mid = n // 2
    return s[mid] if n % 2 == 1 else (s[mid - 1] + s[mid]) / 2.0


def _session_duration(path):
    """单个会话文件的耗时（秒）：事件流最大 time − 最小 time（毫秒 → 秒）。

    事件行顶层有 "time"（毫秒时间戳）；session 事件无 time 时用 createdAt 兜底。
    解析失败 / 无有效时间 → 返回 None（静默，不抛异常）。
    """
    try:
        lines, _ = sp.tail_session(path, 0)
    except Exception:
        return None
    times = []
    for ln in lines:
        try:
            ev = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue
        t = ev.get("time")
        if isinstance(t, (int, float)) and t > 0:
            times.append(t)
        elif ev.get("type") == "session":
            ct = ev.get("createdAt")  # 首行 session 事件无 time 时的兜底
            if isinstance(ct, (int, float)) and ct > 0:
                times.append(ct)
    if not times:
        return None
    dur = (max(times) - min(times)) / 1000.0
    return dur if dur > 0 else None


def compute_hist_s(cwd, exclude_session):
    """同 cwd 目录下历史会话耗时的中位数（秒），排除当前监控会话。

    布局：<SESSIONS_ROOT>/<cwd编码>/<session-id>/session.jsonl.zstd
    （兼容直接平铺在编码目录下的 .zstd）；每个历史会话耗时 = 事件流
    最大 time − 最小 time（_session_duration）。取所有历史会话耗时的
    **中位数**（比均值抗异常值）作为 hist_s。
    扫描失败 / 无历史会话 / cwd 缺失 → 返回 0（静默，不崩溃）。
    结果缓存：会话目录 mtime 无变化时直接复用 hist_s（4s 轮询不重复解压）。
    """
    try:
        if cwd:
            cwd_dir = os.path.join(sp.SESSIONS_ROOT, sp.encode_cwd(cwd))
        elif exclude_session:
            # cwd 缺失时从当前会话路径反推：<root>/<cwd编码>/<session-id>/session.jsonl.zstd
            cwd_dir = os.path.dirname(os.path.dirname(exclude_session))
        else:
            return 0
        if not os.path.isdir(cwd_dir):
            return 0
        try:
            dir_mtime = os.path.getmtime(cwd_dir)
        except OSError:
            dir_mtime = 0
        cached = _HIST_CACHE.get(cwd_dir)
        if cached is not None and cached[0] == dir_mtime:
            return cached[1]  # 目录 mtime 未变 → 复用缓存 hist_s
        durations = []
        try:
            names = os.listdir(cwd_dir)
        except OSError:
            names = []
        for name in names:
            p = os.path.join(cwd_dir, name)
            cands = []
            if os.path.isdir(p):
                cands.append(os.path.join(p, "session.jsonl.zstd"))
            elif name.endswith(".zstd"):
                cands.append(p)  # 兼容直接平铺在编码目录下的会话文件
            for f in cands:
                if not os.path.isfile(f) or f == exclude_session:
                    continue
                dur = _session_duration(f)
                if dur:
                    durations.append(dur)
        hist_s = _median(durations) if durations else 0
        _HIST_CACHE[cwd_dir] = (dir_mtime, hist_s)
        return hist_s
    except Exception:
        return 0  # 历史会话扫描任何失败静默 → 视为无历史


def blend_eta(state, hist_s):
    """ETA 融合：eta = α·linear_s + (1−α)·hist_s（α 随阶段进度自适应）。

    线性外推 linear_s：已走过阶段均速 × 剩余阶段数（marks[0]=任务起点，
    此后每次阶段变化追加时间戳；与旧 compute_eta 逻辑一致）。
    回退链（保持现有字段语义）：
      - 有阶段信息（k≥2 且 n>k）且历史可用 → blend（k≥3 → α=0.7；k==2 → α=0.5）
      - 有阶段信息但无历史 → 纯 linear（α=1）
      - 无阶段信息但有历史 → 纯 history（α=0）
      - 都无 → none
    返回 (eta_s, mode)；hist_s 为 0 / None 视为历史不可用。
    """
    now = time.time()
    start = state.get("started_at") or now
    elapsed = now - start
    marks = state.get("stage_marks") or []
    k = state.get("stage_idx", 0)
    n = state.get("stage_total", 0)
    has_hist = bool(hist_s)  # hist_s > 0 视为历史可用（0 = 无历史 / 扫描失败）
    # 线性外推：已走过阶段均速 × 剩余阶段数
    linear_s = None
    if k >= 2 and n > k and len(marks) >= 2:
        walked = k - 1  # 已走过阶段数（从 marks[0] 到 marks[-1] 之间）
        span = marks[-1] - marks[0] if marks[-1] > marks[0] else elapsed
        per_stage = span / walked if walked > 0 else 0
        if per_stage > 1.0:
            linear_s = per_stage * (n - k)
    if linear_s is not None and has_hist:
        alpha = 0.7 if k >= 3 else 0.5  # k==2 → 0.5
        return (alpha * linear_s + (1 - alpha) * hist_s, "blend")
    if linear_s is not None:
        return (linear_s, "linear")
    if has_hist:
        return (hist_s, "history")
    return (None, "none")


def poll_once():
    """单次轮询：全库扫描最近任务 → 更新共享状态。任何异常静默。"""
    try:
        tasks = scan_tasks()
        with _LOCK:
            _STATE["tasks"] = tasks
    except Exception:
        pass  # 扫描/解析失败全部静默，绝不崩溃


def poll_loop():
    """后台轮询线程：立即刷一次，之后每 POLL_INTERVAL 秒刷新。"""
    while True:
        try:
            poll_once()
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)


def live_payload():
    """/api/live 响应：最近任务列表 JSON（多任务分栏）。

    兼容说明：原单任务顶层字段（running/tag/task/...）不再返回，
    前端已同步改为 {"tasks": [...]}；无任务时 tasks=[]。
    """
    with _LOCK:
        tasks = list(_STATE["tasks"])
    return {"tasks": tasks}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静默

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            try:
                with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
                    body = f.read().encode("utf-8")
            except OSError:
                body = b"index.html missing"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/live":
            self._json(live_payload())
            return
        self.send_response(404)
        self.end_headers()


def build_parser():
    """命令行参数解析器（argparse）。

    位置参数 port 兼容旧的 `python dashboard.py 8123` 用法（缺省 8123）；
    --no-open 关闭启动后自动打开浏览器的行为。
    """
    parser = argparse.ArgumentParser(
        description="dsh 进度可视化看板服务器（纯本地）")
    parser.add_argument("port", nargs="?", type=int, default=8123,
                        help="监听端口（默认 8123；被占用时自动 +1 递增，最多 5 次）")
    parser.add_argument("--no-open", action="store_true",
                        help="启动后不自动打开浏览器（默认自动打开）")
    return parser


def _port_free(port):
    """端口空闲检测：能否绑定 127.0.0.1:port（能绑定即空闲）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def pick_free_port(start, tries=5):
    """端口冲突自动递增：start 被占用时自动 +1 重试（最多 tries 次）。

    找到空闲端口即返回实际端口；每次切换打印「端口 X 被占用，改用 Y」；
    连续 tries 个端口全部被占用 → SystemExit 报错退出（不捕获即进程退出）。
    """
    for i in range(tries):
        p = start + i
        if _port_free(p):
            return p
        if i + 1 < tries:
            print(f"端口 {p} 被占用，改用 {p + 1}", flush=True)
    raise SystemExit(f"端口 {start}~{start + tries - 1} 全部被占用，启动失败")


if __name__ == "__main__":
    args = build_parser().parse_args()
    port = pick_free_port(args.port)  # 端口冲突自动递增（最多 5 次）
    threading.Thread(target=poll_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dsh progress viz: http://127.0.0.1:{port}", flush=True)
    if not args.no_open:
        webbrowser.open(f"http://127.0.0.1:{port}")  # 启动成功后自动打开浏览器
    srv.serve_forever()
