#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh 会话事件流 → 任务进度阶段解析器（纯本地解析，不烧 token）

数据源：C:\\Users\\<user>\\.dsh\\sessions\\<cwd编码>\\<session-id>\\session.jsonl.zstd
（zstd 压缩的 JSONL，任务运行中实时追加写入；每行一个事件，type 字段区分类型）

事件类型（本模块关心）：
  - todo/write：模型写入任务清单（data.todos/items，每项有 content/title + status）
    → 阶段数据源（取第一个未完成项为当前阶段）
  - step/start：步骤边界（data 形如 {"turn":1,"step":1}）→ 无 todo 时的兜底计数
  - tool/call：工具调用（data.name + data.arguments JSON 字符串）→ 最近动作
  - 其他（tool/result、assistant/message、reasoning-chunks、session 等）忽略

⚠️ 解压必须用 zstandard 的 stream_reader（decompressobj 只能解第一个 frame）。
"""
import json
import os

import zstandard

SESSIONS_ROOT = os.path.join(os.path.expanduser("~"), ".dsh", "sessions")
_READ_CHUNK = 1 << 20  # 增量读取块大小（1 MiB）


def encode_cwd(cwd):
    """把绝对路径编码成会话目录名。

    规则：盘符冒号去掉、\\ 和 / 变成 -，首尾加 --。
    例：C:\\Users\\demo\\my-project → --C-Users-demo-my-project--
    """
    s = os.path.normpath(cwd).replace(":", "").replace("\\", "-").replace("/", "-")
    return "--" + s + "--"


def find_latest_session(cwd):
    """返回最新 session.jsonl.zstd 的绝对路径；会话目录不存在返回 None。

    布局：<SESSIONS_ROOT>/<cwd编码>/<session-id>/session.jsonl.zstd
    （兼容个别把 zstd 直接放在编码目录下的情况），按 mtime 取最新。
    """
    sdir = os.path.join(SESSIONS_ROOT, encode_cwd(cwd))
    if not os.path.isdir(sdir):
        return None
    latest, latest_mtime = None, -1.0
    try:
        names = os.listdir(sdir)
    except OSError:
        return None
    for name in names:
        p = os.path.join(sdir, name)
        cands = []
        if os.path.isdir(p):
            cands.append(os.path.join(p, "session.jsonl.zstd"))
        elif name.endswith(".zstd"):
            cands.append(p)  # 直接平铺在编码目录下的会话文件
        for f in cands:
            if os.path.isfile(f):
                mt = os.path.getmtime(f)
                if mt > latest_mtime:
                    latest, latest_mtime = f, mt
    return latest


def parse_event(line):
    """单事件行 → (type, data) 或 None（容错坏行：非 JSON / 无 type / 非 dict）。"""
    if not isinstance(line, str):
        return None
    try:
        ev = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(ev, dict) or not ev.get("type"):
        return None
    return (ev["type"], ev.get("data"))


def _read_zstd(path):
    """完整解压 zstd（多 frame 流式，逐块读取）。

    文件正在写、末尾 frame 不完整时：返回已解出的完整部分（容忍，不抛异常）。
    """
    with open(path, "rb") as f:
        reader = zstandard.ZstdDecompressor().stream_reader(f)
        chunks = []
        while True:
            try:
                chunk = reader.read(_READ_CHUNK)
            except zstandard.ZstdError:
                break  # 末尾 frame 未写完（正在写入）→ 取已解出部分
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def tail_session(path, seen_lines):
    """增量读取：每次全量解压，跳过已处理的行数（seen_lines）。

    返回 (新行列表, 总行数)。文件正在写/尾部不完整/读取失败时静默容忍
    （返回空列表，不抛异常）；尾部半行（无换行结尾）丢弃。
    """
    try:
        raw = _read_zstd(path)
    except Exception:
        return [], 0
    text = raw.decode("utf-8", errors="replace")
    # 尾部不完整（正在写）：最后一行没有换行结尾 → 视为半行，丢弃
    if text and not text.endswith("\n"):
        cut = text.rfind("\n")
        text = text[: cut + 1] if cut >= 0 else ""
    lines = [ln for ln in text.split("\n") if ln]
    total = len(lines)
    if total <= seen_lines:
        return [], total
    return lines[seen_lines:], total


def _item_text(item):
    """任务清单项 → 标题文本（兼容 content/title 两种字段名）。"""
    if isinstance(item, dict):
        return item.get("content") or item.get("title") or ""
    return str(item)


def _pick_todo_stage(items, todo_count):
    """从任务清单选当前阶段项 → (1-based idx, total)。

    规则（按 spec）：
      - 有 status 字段：取第一个未完成项为当前阶段（completed 跳过；
        in_progress/pending 都算当前）；全部完成时取最后一项（任务收尾）。
      - 无 status 字段：取清单第 (已写次数-1) 项（1-based，越界钳制到 [1, total]）。
    """
    total = len(items)
    has_status = any(isinstance(it, dict) and "status" in it for it in items)
    if has_status:
        for i, it in enumerate(items, 1):
            if isinstance(it, dict) and it.get("status") != "completed":
                return i, total
        return total, total  # 全部完成 → 显示最后一项
    pos = max(1, min(todo_count - 1, total))
    return pos, total


def format_action(tool_call):
    """工具调用（tool/call 的 data dict）→ 简短中文动作描述。

    规则（按 spec）：bash 取命令前 60 字符；read/write 取文件名；其他取名称。
    例：「运行 bash 命令: pytest ...」「读取文件: AGENTS.md」「grep」
    """
    if not isinstance(tool_call, dict):
        return None
    name = str(tool_call.get("name") or "")
    if not name:
        return None
    args = tool_call.get("arguments")
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                args = parsed
        except (json.JSONDecodeError, ValueError):
            pass  # arguments 不是 JSON → 按原样处理
    if name == "bash":
        cmd = ""
        if isinstance(args, dict):
            cmd = args.get("command") or args.get("cmd") or ""
        elif isinstance(args, str):
            cmd = args
        return "运行 bash 命令: " + str(cmd)[:60]
    if name in ("read", "write"):
        fp = args.get("file_path") if isinstance(args, dict) else None
        if fp:
            verb = "读取文件" if name == "read" else "写入文件"
            return f"{verb}: {os.path.basename(str(fp).replace(chr(92), chr(47)))}"
        return name
    return name


def build_progress(events):
    """从事件流提取阶段状态，返回 dict：

      {stage: 当前阶段名, stage_idx: 当前第几项, stage_total: 清单总项数,
       action: 最近工具动作摘要}

    规则（按 spec）：
      - 优先 todo/write 的清单（取第一个未完成项；idx 从 1 开始）；
      - 无 todo（或清单不可用）时回退到 step/start 计数：
        stage=「步骤 N」，idx=N，total=0 表示未知；
      - action 取最近 tool/call 的简短中文描述。
    """
    stage, stage_idx, stage_total = None, 0, 0
    todo_count, todo_items, step_count, last_tool = 0, None, 0, None
    for typ, data in events:
        if typ == "todo/write" and isinstance(data, dict):
            todo_count += 1
            items = data.get("items") or data.get("todos")
            if isinstance(items, list) and items:
                todo_items = items
        elif typ == "step/start":
            step_count += 1
        elif typ == "tool/call" and isinstance(data, dict):
            last_tool = data  # 最近一次工具调用
    if todo_items:
        idx, total = _pick_todo_stage(todo_items, todo_count)
        stage_idx, stage_total = idx, total
        stage = _item_text(todo_items[idx - 1])
    if not stage and step_count:
        # 无 todo → step/start 计数兜底
        stage, stage_idx, stage_total = f"步骤{step_count}", step_count, 0
    action = format_action(last_tool) if last_tool is not None else None
    return {"stage": stage, "stage_idx": stage_idx,
            "stage_total": stage_total, "action": action}
