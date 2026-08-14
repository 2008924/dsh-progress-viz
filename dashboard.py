#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh 进度可视化 —— 独立看板服务器（零 dsh-project 依赖，可单独运行）

不依赖 dsh_dispatch.py / running.json：直接监控 ~/.dsh/sessions 下**所有**
cwd 编码目录，取全库最新 mtime 的 session.jsonl.zstd 作为"当前任务"，
每 4 秒增量解析会话事件流，实时呈现阶段 / ETA / 最近动作 / 事件流。

用法: python dashboard.py [port]   (默认 8123)
  浏览器打开 http://127.0.0.1:8123

纯本地运行，任何解压/解析失败都静默容忍，绝不崩溃。
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import session_progress as sp

HERE = os.path.dirname(os.path.abspath(__file__))

POLL_INTERVAL = 4.0   # 轮询间隔（秒）：增量解析会话事件流
TAIL_LINES = 15       # tail 事件流条数（最近 ~15 个事件）
TAIL_CUT = 80         # 单条事件文本截断长度（字符）
_MAX_EVENTS = 10000   # 累积事件封顶（防长会话内存无限增长）

# —— 共享状态：后台轮询线程写入，HTTP 线程只读（_LOCK 保护）——
_LOCK = threading.Lock()
_STATE = {
    "running": False,
    "path": None,          # 当前会话文件路径（会话切换时重置增量状态）
    "tag": None,           # 会话 id 前 8 位（去掉 "session-" 前缀后）
    "task": None,          # 会话 cwd（当作任务描述展示）
    "started_at": None,    # epoch 秒（session 事件 createdAt，缺失用首见时间）
    "seen": 0,             # 已读行数（增量解析游标）
    "events": [],          # 累积解析事件（封顶 _MAX_EVENTS）
    "stage": None, "stage_idx": 0, "stage_total": 0, "stage_pct": 0,
    "action": None,        # 最近工具动作摘要
    "stage_marks": [],     # 阶段变化时间戳（ETA 线性外推：marks[0]=任务起点）
    "eta_s": None, "eta_mode": "none", "eta_at": None,
    "tail": [],            # 最近 ~15 条事件的紧凑文本行（实时动态流）
}


def find_latest_session_all():
    """全库扫描：返回最新 mtime 的 session.jsonl.zstd 路径；无会话返回 None。

    布局：<SESSIONS_ROOT>/<cwd编码>/<session-id>/session.jsonl.zstd
    （兼容个别把 zstd 直接平铺在编码目录下的情况），按 mtime 取全库最新。
    """
    root = sp.SESSIONS_ROOT
    if not os.path.isdir(root):
        return None
    latest, latest_mtime = None, -1.0
    try:
        cwd_dirs = os.listdir(root)
    except OSError:
        return None
    for d in cwd_dirs:
        p = os.path.join(root, d)
        cands = []
        try:
            if os.path.isdir(p):
                for name in os.listdir(p):
                    sub = os.path.join(p, name)
                    if os.path.isdir(sub):
                        cands.append(os.path.join(sub, "session.jsonl.zstd"))
                    elif name.endswith(".zstd"):
                        cands.append(sub)  # 直接平铺在编码目录下的会话文件
        except OSError:
            continue
        for f in cands:
            if os.path.isfile(f):
                mt = os.path.getmtime(f)
                if mt > latest_mtime:
                    latest, latest_mtime = f, mt
    return latest


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


def format_tail_line(ev):
    """单个事件 → 紧凑单行文本（事件类型 + 关键内容，截断 TAIL_CUT 字符）。

    例：「tool/call bash: pytest -q」「assistant/message: 正在分析需求」
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
        text = f"tool/call {name}: {detail}"
    elif typ == "assistant/message" and isinstance(data, dict):
        text = f"assistant/message: {_message_text(data)}"
    elif typ == "todo/write" and isinstance(data, dict):
        items = data.get("todos") or data.get("items") or []
        text = f"todo/write: 任务清单 {len(items)} 项"
    elif typ == "step/start" and isinstance(data, dict):
        text = f"step/start: 步骤{data.get('step', '')}"
    return text[:TAIL_CUT]


def compute_eta(state):
    """ETA 线性外推：已走过阶段均速 × 剩余阶段数（无历史均值兜底）。

    借鉴 dsh_dispatch.compute_eta 的线性外推逻辑独立实现：marks[0] 是任务
    起点（session createdAt），此后每次阶段变化追加一个时间戳；已走过阶段数
    = stage_idx - 1，均速 = (最后 mark - 起点) / 已走过阶段数。
    返回 (eta_s, mode)：mode 为 'linear' 或 'none'（数据不足时无 ETA）。
    """
    now = time.time()
    start = state.get("started_at") or now
    elapsed = now - start
    marks = state.get("stage_marks") or []
    k = state.get("stage_idx", 0)
    n = state.get("stage_total", 0)
    if k >= 2 and n > k and len(marks) >= 2:
        walked = k - 1  # 已走过阶段数（从第 1 个 mark 到当前 mark 之间）
        span = marks[-1] - marks[0] if marks[-1] > marks[0] else elapsed
        per_stage = span / walked if walked > 0 else 0
        if per_stage > 1.0:
            return (per_stage * (n - k), "linear")
    return (None, "none")


def _refresh_progress(st):
    """从累积事件刷新阶段/动作/ETA/tail（写入 _STATE 字典）。"""
    prog = sp.build_progress(st["events"])
    stage = prog.get("stage")
    if stage and stage != st["stage"]:
        st["stage_marks"].append(time.time())  # 阶段变化 → 追加时间戳
    st["stage"] = stage
    st["stage_idx"] = prog.get("stage_idx", 0)
    st["stage_total"] = prog.get("stage_total", 0)
    st["action"] = prog.get("action")
    n = st["stage_total"]
    st["stage_pct"] = round(st["stage_idx"] / n * 100) if n else 0
    st["eta_s"], st["eta_mode"] = compute_eta(st)
    st["eta_at"] = (time.strftime("%H:%M:%S",
                    time.localtime(time.time() + st["eta_s"]))
                    if st["eta_s"] is not None else None)
    st["tail"] = [format_tail_line(e) for e in st["events"][-TAIL_LINES:]]


def poll_once():
    """单次轮询：全库扫描 + 增量解析 → 更新共享状态。任何异常静默。"""
    try:
        path = find_latest_session_all()
        with _LOCK:
            st = _STATE
            if not path:
                # 无会话 → running=false，清空旧状态
                st.update(running=False, path=None, tag=None, task=None,
                          started_at=None, seen=0, events=[], stage=None,
                          stage_idx=0, stage_total=0, stage_pct=0,
                          action=None, stage_marks=[], eta_s=None,
                          eta_mode="none", eta_at=None, tail=[])
                return
            if st["path"] != path:
                # 换了新会话（新任务）→ 重置增量状态并解析元信息
                st.update(path=path, seen=0, events=[], stage_marks=[],
                          tag=None, task=None, started_at=None, stage=None,
                          stage_idx=0, stage_total=0, stage_pct=0,
                          action=None, eta_s=None, eta_mode="none",
                          eta_at=None, tail=[])
                lines0, total0 = sp.tail_session(path, 0)
                meta = _meta_from_lines(lines0)
                st["tag"] = meta["tag"]
                st["task"] = meta["task"]
                st["started_at"] = meta["started_at"] or time.time()
                st["stage_marks"].append(st["started_at"])  # 起点 mark
                st["seen"] = max(st["seen"], total0)
                for ln in lines0:
                    ev = sp.parse_event(ln)
                    if ev:
                        st["events"].append(ev)
            else:
                new_lines, total = sp.tail_session(path, st["seen"])
                if total >= st["seen"]:
                    st["seen"] = total
                for ln in new_lines:
                    ev = sp.parse_event(ln)
                    if ev:
                        st["events"].append(ev)
            if len(st["events"]) > _MAX_EVENTS:
                st["events"] = st["events"][-_MAX_EVENTS:]
            st["running"] = True
            _refresh_progress(st)
    except Exception:
        pass  # 解压/解析失败全部静默，绝不崩溃


def poll_loop():
    """后台轮询线程：立即刷一次，之后每 POLL_INTERVAL 秒刷新。"""
    while True:
        try:
            poll_once()
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)


def live_payload():
    """/api/live 响应：当前任务状态 JSON（字段名与现有看板兼容）。"""
    with _LOCK:
        st = dict(_STATE)
    return {
        "running": st["running"],
        "tag": st["tag"],
        "task": st["task"],
        "stage": st["stage"],
        "stage_idx": st["stage_idx"],
        "stage_total": st["stage_total"],
        "stage_pct": st["stage_pct"],
        "action": st["action"],
        "eta_s": st["eta_s"],
        "eta_mode": st["eta_mode"],
        "eta_at": st["eta_at"],
        "elapsed_s": int(time.time() - st["started_at"]) if st["started_at"] else 0,
        "tail": st["tail"],
        "started_at": st["started_at"],
        "timeout_s": None,
    }


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


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
    threading.Thread(target=poll_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dsh progress viz: http://127.0.0.1:{port}", flush=True)
    srv.serve_forever()
