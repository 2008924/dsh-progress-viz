# -*- coding: utf-8 -*-
"""dsh-progress-viz 合成 fixtures 生成器（纯本地，不烧 token）

程序生成一个**模拟**会话的事件列表（无任何真实会话数据）：
  - session ×1、todo/write ×3（增量快照）、step/start ×8、
    tool/call ×6（bash/read/write 混合）、assistant/message ×5，共 23 个事件
写入 tests\fixtures\session-synthetic.jsonl.zstd：
  zstandard 压缩，多 frame 追加写入（每 6 行一个 frame）模拟真实流式写入。

用法: python tests\make_fixtures.py
"""
import json
import os
import sys

import zstandard

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # 使同级 session_progress.py 可导入（encode_cwd）
import session_progress as _sp
FIX_DIR = os.path.join(HERE, "fixtures")
FIX = os.path.join(FIX_DIR, "session-synthetic.jsonl.zstd")

# 模拟会话元信息（全为虚构，禁止真实路径/任务）
SESSION_ID = "session-01234567-89ab-4cde-8f01-23456789abcd"
CWD = "C:\\Users\\demo\\sample-project"
CREATED_MS = 1787000000000  # 虚构时间戳（毫秒）

FRAME_LINES = 6  # 每个 zstd frame 装 6 行（模拟流式追加写入的多个 frame）


def _ev(typ, seq, time_ms, data):
    """构造一行事件 dict（与真实会话格式一致：type/seq/time/data）。"""
    return {"type": typ, "seq": seq, "time": time_ms, "data": data}


def generate_events():
    """生成模拟会话的事件列表（确定性，计数符合 spec 标注）。

    事件序列（共 23 个）：
      session → step/start×8 与 assistant/message×5、tool/call×6
      （read×2 / write×1 / bash×3）穿插，todo/write×3 增量快照收尾。
    """
    events = []
    seq = 0
    t = CREATED_MS
    t_step = 1000  # 每事件时间步进（毫秒）

    def push(typ, data):
        nonlocal seq, t
        events.append(_ev(typ, seq, t, data))
        seq += 1
        t += t_step

    # 1. session（id/createdAt/cwd 在事件顶层，与真实格式一致）
    events.append({"type": "session", "version": 0, "id": SESSION_ID,
                   "createdAt": CREATED_MS, "cwd": CWD, "delegationDepth": 0})
    seq, t = 1, CREATED_MS + t_step

    # 2~9. 前段：读文档 → 任务清单快照1 → 读代码 → 写笔记 → 清单快照2
    push("step/start", {"turn": 1, "step": 1})
    push("assistant/message", {"turn": 1, "step": 1, "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "正在分析需求"}]}})
    push("tool/call", {"turn": 1, "step": 1, "callId": "call_00_read_readme",
                       "name": "read",
                       "arguments": json.dumps({"file_path": CWD + "\\README.md"})})
    push("todo/write", {"todos": [
        {"content": "阅读项目文档与代码", "status": "in_progress"},
        {"content": "实现核心逻辑", "status": "pending"},
        {"content": "编写并运行测试", "status": "pending"}]})
    push("step/start", {"turn": 1, "step": 2})
    push("assistant/message", {"turn": 1, "step": 2, "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "正在设计实现方案"}]}})
    push("tool/call", {"turn": 1, "step": 2, "callId": "call_00_read_main",
                       "name": "read",
                       "arguments": json.dumps({"file_path": CWD + "\\main.py"})})
    push("step/start", {"turn": 1, "step": 3})
    push("tool/call", {"turn": 1, "step": 3, "callId": "call_00_write_notes",
                       "name": "write",
                       "arguments": json.dumps({"file_path": CWD + "\\notes.md"})})
    push("todo/write", {"todos": [
        {"content": "阅读项目文档与代码", "status": "completed"},
        {"content": "实现核心逻辑", "status": "in_progress"},
        {"content": "编写并运行测试", "status": "pending"}]})

    # 10~17. 中段：写代码 → 跑测试 → 清单快照3（第三项 in_progress）
    push("step/start", {"turn": 1, "step": 4})
    push("assistant/message", {"turn": 1, "step": 4, "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "正在编写代码"}]}})
    push("tool/call", {"turn": 1, "step": 4, "callId": "call_00_bash_test",
                       "name": "bash",
                       "arguments": json.dumps({"command": "pytest -q"})})
    push("step/start", {"turn": 1, "step": 5})
    push("tool/call", {"turn": 1, "step": 5, "callId": "call_00_bash_run",
                       "name": "bash",
                       "arguments": json.dumps({"command": "python main.py --demo"})})
    push("todo/write", {"todos": [
        {"content": "阅读项目文档与代码", "status": "completed"},
        {"content": "实现核心逻辑", "status": "completed"},
        {"content": "编写并运行测试", "status": "in_progress"}]})

    # 18~23. 尾段：跑测试 → 收尾步骤（最近 tool/call 为 bash git status）
    push("step/start", {"turn": 1, "step": 6})
    push("assistant/message", {"turn": 1, "step": 6, "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "正在运行测试"}]}})
    push("tool/call", {"turn": 1, "step": 6, "callId": "call_00_bash_status",
                       "name": "bash",
                       "arguments": json.dumps({"command": "git status"})})
    push("step/start", {"turn": 1, "step": 7})
    push("assistant/message", {"turn": 1, "step": 7, "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "正在整理总结"}]}})
    push("step/start", {"turn": 1, "step": 8})
    return events


def write_fixture(path=FIX, events=None):
    """把事件列表压缩成多 frame 的 zstd 文件（模拟真实流式追加写入）。

    每 FRAME_LINES 行压缩为一个独立 frame，依次追加写入文件——
    zstandard stream_reader 可全量解压（decompressobj 只能解第一个 frame）。
    """
    if events is None:
        events = generate_events()
    lines = [json.dumps(ev, ensure_ascii=False) + "\n" for ev in events]
    cctx = zstandard.ZstdCompressor()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        for i in range(0, len(lines), FRAME_LINES):
            frame = cctx.compress("".join(lines[i:i + FRAME_LINES]).encode("utf-8"))
            f.write(frame)  # 追加写入下一个 frame
    return path


def generate_hist_session(duration_s, start_ms=None, sid=None):
    """生成一个历史会话的事件列表（session + step/start×3）。

    事件 time 跨度（首尾差）恰好 = duration_s 秒（毫秒时间戳），
    用于 ETA 融合的 hist_s（同目录历史会话耗时中位数）测试。
    """
    if start_ms is None:
        start_ms = CREATED_MS - 3600000  # 默认早于当前会话 1 小时
    if sid is None:
        sid = "session-hist-%d" % start_ms
    dur_ms = int(duration_s * 1000)
    events = [{"type": "session", "version": 0, "id": sid,
               "createdAt": start_ms, "cwd": CWD, "delegationDepth": 0,
               "time": start_ms}]
    n = 4  # session + 3 个 step/start
    for i in range(1, n):
        t = start_ms + dur_ms * i // (n - 1)  # 末事件 time = start + duration_s*1000
        events.append({"type": "step/start", "seq": i, "time": t,
                       "data": {"turn": 1, "step": i}})
    return events


def write_hist_fixtures(root, cwd=CWD, durations=(60, 120)):
    """在 root 下生成多个历史会话 fixture（模拟 ~/.dsh/sessions 目录结构）。

    每个历史会话写到 <root>/<cwd编码>/<session-id>/session.jsonl.zstd，
    事件 time 跨度分别为 durations（秒）。返回生成的会话文件路径列表。
    """
    cwd_dir = os.path.join(root, _sp.encode_cwd(cwd))
    paths = []
    for i, dur in enumerate(durations):
        sid = "session-hist-%04d" % i
        events = generate_hist_session(dur, sid=sid)
        path = os.path.join(cwd_dir, sid, "session.jsonl.zstd")
        write_fixture(path, events)
        paths.append(path)
    return paths


if __name__ == "__main__":
    out = write_fixture()
    n = len(generate_events())
    print(f"[ok] 已生成合成 fixture: {out}（{n} 个事件，多 frame zstd）")
