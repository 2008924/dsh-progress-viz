#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh 进度可视化 —— 多任务分栏看板服务器（零 dsh-project 依赖，可单独运行）

数据源（按优先级，spec 2026-08-15 插件版）：
  1. 插件输出 <DSH_HOME>/progress/*.json（dsh-progress-viz-plugin 实时写入、
     已过滤 chunk 噪音）—— read_progress_json 读取，字段与 /api/live 对齐；
  2. 缺失/无文件 → 回退 zstd 会话解析（scan_session_files，行为不变）。
不依赖 dsh_dispatch.py / running.json：直接监控 ~/.dsh/sessions 下**所有**
cwd 编码目录里的全部会话文件（session.jsonl.zstd），收集为「最近任务列表」：
只保留最近 1 小时内有写入的任务，按文件 mtime 降序取前 8 个；每个任务
独立解析（阶段 / 动作 / ETA / 标题 / 成本 / 事件流 / 时间线），/api/live
返回 {"tasks": [...]}。解析结果带缓存：文件 mtime 未变直接复用（4s 轮询不
重复解压），会话被删除/目录消失时缓存条目自动清理。
ETA 为「阶段线性外推 + 同目录历史会话耗时中位数」加权融合（详见
compute_hist_s / blend_eta），历史会话排除任务自身。
成本为 DeepSeek 定价**估算**（usage tokens × PRICES，字段 cost_est，
无 usage 数据时为 None 且前端不显示）；任务标题优先 session/title 事件。
飞书完成通知：任务 running→completed 翻转时向 webhook POST 文本消息
（同一会话只通知一次，启动时已 completed 不通知，失败静默）。

用法: python dashboard.py [port]   (默认 8123)
  启动成功后自动用默认浏览器打开 http://127.0.0.1:<实际端口>
  （--no-open 关闭自动打开）；端口被占用时自动 +1 递增（最多 5 次）。
  --status：不启动服务器，直接打印当前任务状态表后退出（exit 0）。
  --feishu-webhook <URL>（或环境变量 FEISHU_WEBHOOK）：启用飞书完成通知；
  --no-feishu 强制关闭。

纯本地运行，任何解压/解析失败都静默容忍，绝不崩溃。
"""
import argparse
import datetime
import json
import os
import socket
import threading
import time
import urllib.request
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
TIMELINE_MAX = 50     # 任务详情时间线条数上限（最多 50 条，取最近）

# DeepSeek 官方定价（元/百万 tokens），deepseek-chat（V3）2025-02 起生效：
#   输入（缓存未命中）¥2、输出 ¥8、缓存命中 ¥0.5；deepseek-reasoner 为 4/16/1。
#   本机会话模型为 deepseek-v4-flash，按 deepseek-chat 价近似估算。
#   价格可改：修改本常量后重启看板即生效（成本为估算值，字段名 cost_est 标注）。
PRICES = {"input": 2.0, "output": 8.0, "cache_read": 0.5}

# —— 插件版数据源（dsh-progress-viz-plugin 输出）——
# <DSH_HOME>/progress/*.json（每次语义事件原子重写，已过滤 chunk 噪音）。
# 测试可重定向本变量（与 sp.SESSIONS_ROOT 同模式）。
PROGRESS_ROOT = os.path.join(
    os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh"),
    "progress")

# —— 共享状态：后台轮询线程写入，HTTP 线程只读（_LOCK 保护）——
_LOCK = threading.Lock()
_STATE = {"tasks": []}   # 最近任务列表快照（scan_tasks 的结果）

# —— 飞书完成通知状态（_NOTIFIED_LOCK 保护，轮询线程写 + 测试并发读）——
_FEISHU_WEBHOOK = None   # 飞书群机器人 webhook URL（--feishu-webhook / 环境变量 FEISHU_WEBHOOK）
_FEISHU_ENABLED = False  # 是否启用通知（有 webhook 且未 --no-feishu）
_NOTIFIED_IDS = set()    # 已通知过的完整会话 id（同一会话只通知一次）
_PREV_STATUS = {}        # {完整会话 id: 上次扫描 status}，用于检测 running→completed 翻转
_NOTIFIED_LOCK = threading.Lock()

# —— 解析缓存：{会话文件路径: (mtime, 解析结果 dict)}；文件 mtime 未变直接复用，
#    避免 4s 轮询对未变化（尤其已完成）的会话文件反复全量解压（_PARSE_CACHE_LOCK
#    保证多线程安全：后台轮询线程写 + 测试/并发读）——
_PARSE_CACHE = {}
_PARSE_CACHE_LOCK = threading.Lock()


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
    task 用事件里的 cwd（比编码目录名更完整）；started_at 用 createdAt(ms)；
    sid 为完整会话 id（供飞书通知去重的私有键，不对外暴露）。
    返回 {"tag": ..., "task": ..., "started_at": epoch 秒或 None, "sid": ...}。
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
            return {"tag": tag, "task": ev.get("cwd"),
                    "started_at": started, "sid": sid or None}
    return {"tag": None, "task": None, "started_at": None, "sid": None}


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


def scan_usage(events):
    """探测并累计事件流里的 token usage → 成本估算输入；无 usage 返回 None。

    实测（2026-08-15 本机真实会话）：usage 位于 assistant/message 事件的
    data.usage，键为 inputTokens/outputTokens/cacheReadTokens/reasoningTokens
    （camelCase）；兼容 tool/result 与 snake_case（prompt_tokens/completion_tokens）。
    返回 {"input_tokens": int, "output_tokens": int, "cache_read_tokens": int}；
    事件流无任何 usage 数据 → None（cost_est 置 None，前端不显示，禁止硬编假数据）。
    """
    total_in = total_out = total_cache = 0
    found = False
    for typ, data in events:
        if typ not in ("assistant/message", "tool/result") \
                or not isinstance(data, dict):
            continue
        u = data.get("usage")
        if not isinstance(u, dict):
            continue
        tin = u.get("inputTokens", u.get("prompt_tokens", 0))
        tout = u.get("outputTokens", u.get("completion_tokens", 0))
        tc = u.get("cacheReadTokens", 0)
        if not isinstance(tin, (int, float)) or not isinstance(tout, (int, float)):
            continue  # usage 字段格式不符 → 跳过该事件（不当作有效 usage）
        total_in += int(tin or 0)
        total_out += int(tout or 0)
        if isinstance(tc, (int, float)):
            total_cache += int(tc or 0)
        found = True
    if not found:
        return None
    return {"input_tokens": total_in, "output_tokens": total_out,
            "cache_read_tokens": total_cache}


def estimate_cost(usage):
    """按 DeepSeek 定价常量把 usage 估算为成本（元）；usage 为 None → None。

    公式：input×输入价 + output×输出价 + cache_read×缓存价，除以 1e6
    （PRICES 单位为元/百万 tokens）。返回浮点元，展示侧格式化为「≈¥0.0123」；
    无 usage → None（前端不显示成本行）。
    """
    if not usage:
        return None
    yuan = (usage.get("input_tokens", 0) * PRICES["input"]
            + usage.get("output_tokens", 0) * PRICES["output"]
            + usage.get("cache_read_tokens", 0) * PRICES.get("cache_read", 0.0))
    return yuan / 1_000_000.0


def _task_title(events, cwd):
    """任务标题：优先 session/title 事件的 data.title（取最后一个非空）。

    实测本机会话会有多个 session/title 事件（先 fallback 摘要、后 provider
    生成版，后者更完整）→ 取最后一个非空值；无标题回退 cwd 的 basename，
    再回退完整 cwd；都无 → None。
    """
    title = None
    for typ, data in events:
        if typ == "session/title" and isinstance(data, dict):
            t = data.get("title")
            if isinstance(t, str) and t.strip():
                title = t.strip()
    if title:
        return title
    if cwd:
        base = os.path.basename(cwd.rstrip("\\/"))
        return base or cwd
    return None


def _tool_call_detail(data):
    """tool/call 的 data → 工具名 + 参数摘要文本（如「bash: pytest -q」）。

    bash 取命令原文；read/write 取 file_path；其他取 arguments JSON 摘要。
    供 format_tail_line / _timeline_desc 复用（参数解析逻辑只写一份）。
    """
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
    return f"{name}: {detail}"


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
        text = f"tool/call {_tool_call_detail(data)}"  # 突出动作：工具名 + 参数摘要
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


# 语义事件白名单（显示层只保留这些事件，与插件 SEMANTIC_TYPES 一致）：
#   todo/write、step/start、step/end、tool/call、tool/result、assistant/message、
#   turn/start、turn/end、session/title、session。
# 其余 chunk 类中间态事件（assistant/chunk、reasoning-chunks、tool-call-chunks、
# text-chunks、agent/inbox/spliced、request/* 等）占事件流绝大多数且内容无差别，
# 一律**过滤**（不再合并展示），避免 tail/timeline 被刷屏。
_SEMANTIC_TYPES = frozenset((
    "todo/write", "step/start", "step/end", "tool/call", "tool/result",
    "assistant/message", "turn/start", "turn/end", "session/title", "session"))


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
    """事件流 → 可读性优化后的 tail 行列表（最多 max_lines 条，**倒序**：最新在上）。

    规则（按 spec fix-bugs）：
      - 显示层**过滤**所有 chunk 类事件（assistant/chunk、reasoning-chunks、
        tool-call-chunks、text-chunks 等），只保留语义事件（todo/write、
        step/start、step/end、tool/call、tool/result、assistant/message、
        turn/start、turn/end、session/title、session），不再刷屏；
      - tool/call 突出为「tool/call 工具名: 参数摘要」（复用 format_action 思路）；
      - todo/write 显示「todo: 当前第 k 项/共 n 项」；
      - 每条前加 [HH:MM:SS]（time 字段毫秒时间戳 → 本地时间，times 已换算为秒）；
        无 time 字段用文件事件顺序序号 [N] 代替；
      - 保留最近 max_lines 条（过滤后计数），并**倒序**返回（最新事件在最上面）。

    events: parse_event 的 (type, data) 列表；times: 平行的 epoch 秒列表
    （None 表示该事件无 time），与 _read_task 的返回约定一致。
    """
    if max_lines <= 0:
        return []
    kept = []  # 过滤后的 (时间戳文本, 行文本) 列表（按事件顺序）
    todo_count = 0
    i, n = 0, len(events)
    while i < n:
        ev = events[i]
        typ = ev[0] if isinstance(ev, (tuple, list)) and ev else None
        if typ not in _SEMANTIC_TYPES:
            i += 1  # 过滤 chunk 等中间态噪音（不显示、不计数）
            continue
        if typ == "todo/write":
            todo_count += 1
        kept.append((_ts_text(i, times), format_tail_line(ev, todo_count)))
        i += 1
    # 倒序：最新事件在最上面（先保留最近 max_lines 条，再反转）
    return [ts + " " + text for ts, text in kept[-max_lines:]][::-1]


def _timeline_desc(ev, todo_count):
    """单个事件 → 时间线描述文本（不含类型前缀；type 字段单独列在条目里）。

    与 format_tail_line 同规则，但去掉「tool/call」「assistant/message」等
    类型前缀，避免时间线条目里类型重复显示。
    """
    typ, data = ev
    if typ == "tool/call" and isinstance(data, dict):
        return _tool_call_detail(data)
    if typ == "assistant/message" and isinstance(data, dict):
        return _message_text(data)
    if typ == "todo/write" and isinstance(data, dict):
        items = data.get("todos") or data.get("items") or []
        if isinstance(items, list) and items:
            idx, total = sp._pick_todo_stage(items, todo_count)
            return f"当前第 {idx} 项/共 {total} 项"
        return ""
    if typ == "step/start" and isinstance(data, dict):
        return f"步骤{data.get('step', '')}"
    if typ == "session/title" and isinstance(data, dict):
        return str(data.get("title") or "")
    return ""


def build_timeline(events, times=None, max_items=TIMELINE_MAX):
    """事件流 → 任务详情时间线摘要列表（[{t, type, desc}]，最多 max_items 条）。

    与 format_tail 同规则（过滤 chunk 等中间态噪音 / todo 进度 / tool 摘要），
    但保留结构化字段供前端渲染：t 为 HH:MM:SS（无 time 用文件事件顺序序号），
    type 为事件类型，desc 为不含类型前缀的简短描述。取**最近** max_items 条
    （时间线随任务推进滚动，上限 50 条防页面过载）。**保持事件正序**返回，
    展示倒序由前端 timelineHtml 处理（最新在上）。
    """
    if max_items <= 0:
        return []
    items = []
    todo_count = 0
    i, n = 0, len(events)
    while i < n:
        ev = events[i]
        typ = ev[0] if isinstance(ev, (tuple, list)) and ev else None
        if typ not in _SEMANTIC_TYPES:
            i += 1  # 过滤 chunk 等中间态噪音（不显示、不计数）
            continue
        ts = _ts_text(i, times)
        t = ts[1:-1] if len(ts) >= 3 and ts[0] == "[" and ts[-1] == "]" else ts
        if typ == "todo/write":
            todo_count += 1
        items.append({"t": t, "type": typ,
                      "desc": _timeline_desc(ev, todo_count)})
        i += 1
    return items[-max_items:]


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


def _parse_task(path):
    """单个会话文件 → 解析结果 dict（不含依赖 now 的 status/elapsed_s/eta_at）。

    复用现有解析函数：_meta_from_lines（id/cwd）、sp.build_progress（阶段/动作）、
    blend_eta（ETA）、format_tail_line（事件流紧凑文本）、scan_usage（成本）、
    build_timeline（详情时间线）；解析失败返回 None（静默跳过，不崩溃）。
    结果带私有键 _started_at（供 _build_task 刷新 elapsed_s）与 _sid（供飞书
    通知去重，live_payload 剔除），对外字段与旧 _build_task 输出一致。
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
            "title": _task_title(events, cwd),  # 任务标题（session/title 优先）
            "stage": prog.get("stage"),
            "stage_idx": k,
            "stage_total": n,
            "stage_pct": round(k / n * 100) if n else 0,
            "action": prog.get("action"),
            "eta_s": eta_s,
            "eta_mode": eta_mode,
            "eta_at": eta_at,
            "cost_est": estimate_cost(scan_usage(events)),  # 成本估算（无 usage → None）
            "timeline": build_timeline(events, times),      # 详情时间线（最多 50 条）
            "tail": format_tail(events, times, TAIL_LINES),
            "_started_at": started_at,  # 私有键：仅供 elapsed_s 刷新，不对外
            "_sid": meta["sid"],        # 私有键：仅供飞书通知去重，不对外
        }
    except Exception:
        return None  # 单个任务解析失败静默跳过


def _parse_task_cached(path):
    """解析缓存入口：文件 mtime 未变 → 直接复用缓存结果（不重新解压）。

    mtime 变了 / 新文件 / 缓存被清理 → 重新解压解析并更新缓存；
    文件已不存在（删除/目录消失）→ 返回 None 并清理对应缓存条目。
    缓存读改写均受 _PARSE_CACHE_LOCK 保护（多线程安全）；解压解析放在
    锁外执行，避免持锁做 IO。
    """
    try:
        mt = os.path.getmtime(path)
    except OSError:
        with _PARSE_CACHE_LOCK:
            _PARSE_CACHE.pop(path, None)
        return None
    with _PARSE_CACHE_LOCK:
        hit = _PARSE_CACHE.get(path)
        if hit is not None and hit[0] == mt:
            return hit[1]  # mtime 未变 → 复用缓存解析结果
    parsed = _parse_task(path)
    if parsed is None:
        return None
    with _PARSE_CACHE_LOCK:
        _PARSE_CACHE[path] = (mt, parsed)
    return parsed


def _prune_parse_cache(alive_paths):
    """扫描后清理解析缓存：不在本次扫描结果里的 key 直接移除。

    会话被删除 / cwd 目录消失 → 文件不再被扫描到 → 对应缓存条目随之
    失效（只保留仍存在的会话文件，缓存不会无限增长）。
    """
    with _PARSE_CACHE_LOCK:
        stale = [p for p in _PARSE_CACHE if p not in alive_paths]
        for p in stale:
            _PARSE_CACHE.pop(p, None)


def _build_task(path, status, now):
    """单个会话文件 → 任务对象 dict；解析失败返回 None（静默跳过，不崩溃）。

    解析结果走 _parse_task_cached 缓存（文件 mtime 未变复用，不重复解压）；
    仅依赖当前时刻的 status / elapsed_s / eta_at 每次按 now 刷新，与全量
    解析的字段语义完全一致（/api/live 返回结构不变）。
    """
    try:
        parsed = _parse_task_cached(path)
        if parsed is None:
            return None
        task = dict(parsed)  # 浅拷贝，不污染缓存
        task["status"] = status
        task["elapsed_s"] = int(now - task.pop("_started_at"))
        if task.get("eta_s") is not None:
            # eta_at = 预计完成时刻：按当前 now 刷新（与全量解析一致）
            task["eta_at"] = time.strftime("%H:%M:%S",
                                           time.localtime(now + task["eta_s"]))
        return task
    except Exception:
        return None  # 单个任务解析失败静默跳过


def _plugin_task(path, data, now):
    """单个插件进度 JSON → 任务 dict（字段与 zstd 解析结果对齐）。

    字段语义（/api/live 不变）：
      - id：session_id 去 "session-" 前缀后前 8 位（与 zstd 路径同规则）；
      - status：finished → completed；未结束时沿用 mtime 年龄语义
        （≤ RUNNING_AGE → running，更旧 → completed）；
      - tail：由 timeline 派生（"[HH:MM:SS] type: desc" 行，取最近 TAIL_LINES 条，
        **倒序**：最新事件在最上面，与 zstd 路径 format_tail 一致）；
      - eta_s/eta_mode/eta_at：插件 JSON 无阶段时间戳 → None（前端不显示）；
      - elapsed_s：以 JSON 的 updated_at 与 elapsed_s 反推 started_at，
        按当前 now 刷新（与 zstd 路径的刷新语义一致）。
    """
    sid = data.get("session_id") or ""
    tag = sid[8:16] if sid.startswith("session-") else (sid[:8] or None)
    timeline = data.get("timeline")
    if not isinstance(timeline, list):
        timeline = []
    tail = []
    for it in reversed(timeline[-TAIL_LINES:]):  # 倒序：最新事件在最上面
        if isinstance(it, dict):
            line = "[%s] %s: %s" % (it.get("t") or "", it.get("type") or "",
                                    it.get("desc") or "")
            tail.append(line.strip()[:TAIL_CUT])
    finished = bool(data.get("finished"))
    try:
        age = now - os.path.getmtime(path)
    except OSError:
        age = now
    status = "completed" if finished or age > RUNNING_AGE else "running"
    # elapsed_s 刷新：started_at ≈ updated_at − 写时 elapsed_s
    elapsed = data.get("elapsed_s")
    if not isinstance(elapsed, (int, float)) or elapsed < 0:
        elapsed = 0
    updated = data.get("updated_at")
    if isinstance(updated, str) and updated:
        try:
            upd_ts = datetime.datetime.fromisoformat(
                updated.replace("Z", "+00:00")).timestamp()
            elapsed = int(now - (upd_ts - float(elapsed)))
        except (ValueError, TypeError):
            elapsed = int(elapsed)
    else:
        elapsed = int(elapsed)
    return {
        "id": tag,
        "cwd": data.get("cwd"),
        "title": data.get("title"),
        "status": status,
        "stage": data.get("stage"),
        "stage_idx": data.get("stage_idx") or 0,
        "stage_total": data.get("stage_total") or 0,
        "stage_pct": data.get("stage_pct") or 0,
        "action": data.get("action"),
        "eta_s": None,       # 插件 JSON 无阶段时间戳 → ETA 不估算
        "eta_mode": None,
        "eta_at": None,
        "cost_est": data.get("cost_est"),
        "timeline": timeline,
        "tail": tail,
        "elapsed_s": elapsed,
        "_sid": sid,         # 私有键：仅供飞书通知去重，不对外
    }


def read_progress_json(now=None):
    """读取插件输出的进度 JSON（<DSH_HOME>/progress/*.json，数据源优先级 1）。

    - 只读 per-session 文件（排除 current.json：它是最新会话的指针，
      与 per-session 文件内容重复，避免同一任务出现两次）；
    - 按文件 mtime 降序，只保留最近 RECENT_WINDOW 秒内有更新的文件，
      取前 MAX_TASKS 个（与 zstd 扫描同一窗口/上限语义）；
    - 单个文件读取/解析失败静默跳过（损坏文件不崩溃）；
    - 目录不存在或无文件 → []（调用方回退 zstd 解析）。
    """
    if now is None:
        now = time.time()
    root = PROGRESS_ROOT
    if not os.path.isdir(root):
        return []
    try:
        names = os.listdir(root)
    except OSError:
        return []
    files = []
    for name in names:
        if not name.endswith(".json") or name == "current.json":
            continue
        p = os.path.join(root, name)
        try:
            if os.path.isfile(p):
                files.append((p, os.path.getmtime(p)))
        except OSError:
            continue
    files.sort(key=lambda x: x[1], reverse=True)  # 按 mtime 降序
    tasks = []
    for path, mt in files:
        age = now - mt
        if age >= RECENT_WINDOW:
            break  # 已按 mtime 降序 → 之后只会更旧 → 直接结束
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not data.get("session_id"):
                continue
            task = _plugin_task(path, data, now)
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            continue  # 损坏文件静默跳过
        tasks.append(task)
        if len(tasks) >= MAX_TASKS:
            break
    return tasks


def scan_tasks(now=None):
    """全库扫描 → 最近任务列表（多任务分栏数据源）。

    数据源优先级（按 spec 插件版）：
      1. 插件输出 <DSH_HOME>/progress/*.json（实时、已过滤噪音）——
         read_progress_json 读取，字段与 /api/live 对齐；
      2. 缺失/无文件 → 回退现有 zstd 会话解析（行为不变）。
    插件数据路径说明：
      - 收集插件 JSON，只保留最近 1 小时内有写入（mtime 距今 < 3600s）的；
      - 按 mtime 降序取前 MAX_TASKS 个；status 由 finished 标志 + mtime
        年龄判定（finished → completed，否则 ≤30s → running）；
      - 单个文件损坏静默跳过。
    zstd 回退路径说明（原逻辑不变）：
      - 收集全部会话文件，只保留最近 1 小时内有写入的；
      - 按 mtime 降序取前 8 个；每个任务独立解析（解析缓存：文件 mtime
        未变直接复用，不重复解压）；status：mtime 距今 ≤ 30s → running，
        否则 completed；
      - 扫描后清理解析缓存：会话被删除/目录消失 → 对应缓存条目随之失效；
      - 单个会话扫描/解析失败静默跳过；无任务返回 []。
    """
    if now is None:
        now = time.time()
    plugin_tasks = read_progress_json(now)
    if plugin_tasks:
        return plugin_tasks  # 插件数据优先（已过滤噪音、实时）
    tasks = []
    files = scan_session_files()
    for path, mt in files:
        age = now - mt
        if age >= RECENT_WINDOW:
            break  # 已按 mtime 降序，之后只会更旧 → 直接结束
        status = "running" if age <= RUNNING_AGE else "completed"
        task = _build_task(path, status, now)
        if task is not None:
            tasks.append(task)
        if len(tasks) >= MAX_TASKS:
            break
    _prune_parse_cache({p for p, _ in files})
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


def _feishu_post(url, text):
    """向飞书群机器人 webhook POST 一条文本消息（失败静默，不抛异常）。

    格式：{"msg_type": "text", "content": {"text": "..."}}（飞书官方格式）。
    这是本工具唯一的网络调用：仅任务翻转时触发，超时 5s，任何失败静默
    （不阻塞看板轮询线程）。
    """
    try:
        body = json.dumps({"msg_type": "text",
                           "content": {"text": text}}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json; charset=utf-8")
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception:
        pass  # 发送失败静默（不阻塞看板）


def _fmt_mmss(seconds):
    """秒 → mm:ss 文本（耗时展示，如 12:34；≥1 小时显示 h:mm:ss）。"""
    s = max(0, int(seconds or 0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def _send_feishu_notify(task):
    """组装完成通知文本并发送（标题/cwd/耗时 mm:ss/最后阶段）。

    文案：「✅ dsh 任务完成：<标题>（<cwd>）· 耗时 <mm:ss> · 阶段 <最后阶段>」；
    标题/阶段截断到合理长度，避免飞书消息过长。
    """
    title = (task.get("title") or task.get("cwd") or "未知任务")[:60]
    cwd = task.get("cwd") or "未知目录"
    stage = task.get("stage") or (
        f"步骤{task.get('stage_idx')}" if task.get("stage_idx") else "未知")
    text = (f"✅ dsh 任务完成：{title}（{cwd}）· "
            f"耗时 {_fmt_mmss(task.get('elapsed_s'))} · 阶段 {str(stage)[:30]}")
    _feishu_post(_FEISHU_WEBHOOK, text)


def _check_flips(tasks):
    """检测 running→completed 翻转 → 触发飞书完成通知（去重 + 静默失败）。

    规则（按 spec）：
      - 只看「上次扫描 running → 本次扫描 completed」的任务；首次扫描无上次
        状态 → **启动时已存在的 completed 任务天然不通知**；
      - 同一会话只通知一次（_NOTIFIED_IDS 缓存完整会话 id，先标记再发送，
        发送失败也不重试 → 保证不重复刷屏）；
      - 未启用（--no-feishu / 无 webhook）→ 只维护状态不发送；
      - 发送失败由 _feishu_post 内部静默（不阻塞看板）。
    状态表 _PREV_STATUS / _NOTIFIED_IDS 受 _NOTIFIED_LOCK 保护（线程安全）。
    """
    with _NOTIFIED_LOCK:
        current = {t["_sid"]: t for t in tasks if t.get("_sid")}
        for sid, task in current.items():
            if (_PREV_STATUS.get(sid) == "running"
                    and task["status"] == "completed"
                    and sid not in _NOTIFIED_IDS):
                _NOTIFIED_IDS.add(sid)  # 先标记：同一会话只通知一次
                if _FEISHU_ENABLED and _FEISHU_WEBHOOK:
                    _send_feishu_notify(task)
        _PREV_STATUS.clear()
        _PREV_STATUS.update({sid: t["status"] for sid, t in current.items()})


def status_text(tasks):
    """任务状态表文本（CLI --status 用）：状态/标题/阶段 k/N/ETA/成本。

    无任务 → 「暂无任务」（exit 0，不启动服务器）。
    """
    if not tasks:
        return "暂无任务"
    lines = ["{:<9}{:<42}{:<12}{:<10}{}".format("状态", "标题", "阶段", "ETA", "成本")]
    for t in tasks:
        status = "running" if t.get("status") == "running" else "completed"
        title = ((t.get("title") or "")[:40] or "-")
        k, n = t.get("stage_idx") or 0, t.get("stage_total") or 0
        stage = f"{k}/{n}" if n else (str(k) if k else "-")
        eta = t.get("eta_at") or "-"
        cost = f"≈¥{t['cost_est']:.4f}" if t.get("cost_est") is not None else "-"
        lines.append("{:<9}{:<42}{:<12}{:<10}{}".format(
            status, title, stage, eta, cost))
    return "\n".join(lines)


def poll_once():
    """单次轮询：全库扫描最近任务 → 更新共享状态 + 飞书翻转检测。任何异常静默。"""
    try:
        tasks = scan_tasks()
        with _LOCK:
            _STATE["tasks"] = tasks
        _check_flips(tasks)
    except Exception:
        pass  # 扫描/解析/通知失败全部静默，绝不崩溃


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
    私有键 _sid（完整会话 id，仅供飞书通知去重）在此剔除，不对外暴露。
    """
    with _LOCK:
        tasks = [dict(t) for t in _STATE["tasks"]]
    for t in tasks:
        t.pop("_sid", None)
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
    --no-open 关闭启动后自动打开浏览器的行为；
    --status 不启动服务器，直接打印当前任务状态表后退出（exit 0）；
    --feishu-webhook / 环境变量 FEISHU_WEBHOOK 配置飞书完成通知
    （任务 running→completed 翻转时 POST 一条文本消息）；--no-feishu 强制关闭。
    """
    parser = argparse.ArgumentParser(
        description="dsh 进度可视化看板服务器（纯本地）")
    parser.add_argument("port", nargs="?", type=int, default=8123,
                        help="监听端口（默认 8123；被占用时自动 +1 递增，最多 5 次）")
    parser.add_argument("--no-open", action="store_true",
                        help="启动后不自动打开浏览器（默认自动打开）")
    parser.add_argument("--status", action="store_true",
                        help="不启动服务器：打印当前任务状态表（状态/标题/阶段/ETA/成本）后退出")
    parser.add_argument("--feishu-webhook", default=None,
                        help="飞书群机器人 webhook URL（任务完成时通知；也可用环境变量 FEISHU_WEBHOOK）")
    parser.add_argument("--no-feishu", action="store_true",
                        help="关闭飞书完成通知（即使配置了 webhook）")
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
    if args.status:
        # --status：不启动服务器，直接打印当前任务状态表（exit 0）
        print(status_text(scan_tasks()))
        raise SystemExit(0)
    # 飞书配置：命令行参数优先，其次环境变量 FEISHU_WEBHOOK；--no-feishu 强制关闭
    _FEISHU_WEBHOOK = args.feishu_webhook or os.environ.get("FEISHU_WEBHOOK") or ""
    _FEISHU_ENABLED = bool(_FEISHU_WEBHOOK) and not args.no_feishu
    if _FEISHU_ENABLED:
        print(f"飞书完成通知已启用: {_FEISHU_WEBHOOK[:48]}...", flush=True)
    port = pick_free_port(args.port)  # 端口冲突自动递增（最多 5 次）
    threading.Thread(target=poll_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"dsh progress viz: http://127.0.0.1:{port}", flush=True)
    if not args.no_open:
        webbrowser.open(f"http://127.0.0.1:{port}")  # 启动成功后自动打开浏览器
    srv.serve_forever()
