# -*- coding: utf-8 -*-
"""dsh-progress-viz tail 可读性优化单测（纯本地，不烧 token）

运行方式：python tests/test_tail_format.py（无需 pytest），exit 0 全过。

覆盖（按 spec fix-bugs）：
  ① chunk 类事件全过滤：assistant/chunk、reasoning-chunks、tool-call-chunks、
     text-chunks 一律不出现，只保留语义事件（todo/write、step/start、step/end、
     tool/call、tool/result、assistant/message、turn/start、turn/end、
     session/title、session）
  ② 只含 chunk 的事件序列 → 空列表（无任何残留行）
  ③ tool/call 显示「tool/call 工具名: 参数摘要」（复用 format_action 思路）
  ④ todo/write 显示「todo: 当前第 k 项/共 n 项」（有 status 取第一个未完成项 /
     无 status 用已写次数规则）
  ⑤ 每条行首 [HH:MM:SS]（time 字段→本地时间）；无 time 用文件事件顺序序号 [N]
  ⑥ 倒序：最新事件在最上面（最近 max_lines 条，取后反转）
  ⑦ max_lines 截断（过滤后计数）；max_lines<=0 → 空列表
"""
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)  # publish\dsh-progress-viz
sys.path.insert(0, PKG)      # 使 dashboard.py 可导入
import dashboard as db

fails = 0

BASE = 1787000000.0  # 固定 epoch 秒：时间戳断言用同一换算（不依赖时区）


def check(name, cond, detail=""):
    global fails
    if cond:
        print(f"  PASS {name}")
    else:
        fails += 1
        print(f"  FAIL {name} {detail}")


def build(seq):
    """seq: (type, time_s, data) 列表 → (events, times) 平行列表（与 _read_task 一致）。

    time_s 为 epoch 秒（None 表示该事件无 time 字段）。
    """
    events, times = [], []
    for typ, t, data in seq:
        events.append((typ, data))
        times.append(t)
    return events, times


def hms(t):
    """epoch 秒 → [HH:MM:SS]（与 dashboard 用同一换算）。"""
    return time.strftime("[%H:%M:%S]", time.localtime(t))


print("== ① chunk 类事件全过滤（只保留语义事件）==")
# 事件序列：reasoning-chunks×10 → tool-call-chunks×3 → assistant-chunks×2 →
# text-chunks×2 → assistant/chunk×1 → tool/call(bash) → todo/write(3 项) →
# assistant/message → step/start(无 time)
seq = [("reasoning-chunks", BASE + i, None) for i in range(10)]
seq += [("tool-call-chunks", BASE + 10 + i, None) for i in range(3)]
seq += [("assistant-chunks", BASE + 13 + i, None) for i in range(2)]
seq += [("text-chunks", BASE + 15 + i, None) for i in range(2)]
seq += [("assistant/chunk", BASE + 17, None)]
seq += [("tool/call", BASE + 18, {"name": "bash",
                                  "arguments": '{"command": "pytest -q"}'})]
seq += [("todo/write", BASE + 19, {"todos": [
    {"content": "步骤A", "status": "completed"},
    {"content": "步骤B", "status": "in_progress"},
    {"content": "步骤C", "status": "pending"}]})]
seq += [("assistant/message", BASE + 20, {"message": {"content": [
    {"type": "text", "text": "正在分析"}]}})]
seq += [("step/start", None, {"turn": 1, "step": 3})]  # 无 time → 序号
events, times = build(seq)
lines = db.format_tail(events, times, max_lines=20)

check("chunk 类事件全部被过滤（无 ×N 合并行）",
      not any("chunk" in ln for ln in lines), str(lines))
check("只保留语义事件（4 条：tool/todo/assistant/step）",
      len(lines) == 4, str(lines))

print("== ⑥ 倒序：最新事件在最上面 ==")
check("最新事件 step/start（无 time → [22]）在最上面",
      lines[0] == "[22] step/start: 步骤3", lines[0])
check("第二行为 assistant/message",
      "assistant/message: 正在分析" in lines[1], lines[1])
check("第三行为 todo（当前第 2 项/共 3 项）",
      "todo: 当前第 2 项/共 3 项" in lines[2], lines[2])
check("最下面为最老语义事件 tool/call bash",
      "tool/call bash: pytest -q" in lines[3], lines[3])
check("有 time 的行首为 [HH:MM:SS]",
      all(re.match(r"^\[\d{2}:\d{2}:\d{2}\] ", ln) for ln in lines[1:]), str(lines))
check("无 time 的 step/start 用文件顺序序号 [22]",
      lines[0].startswith("[22] ") and "step/start" in lines[0], lines[0])

print("== ② 只含 chunk 的事件序列 → 空列表 ==")
only_chunk_ev, _ = build([("reasoning-chunks", BASE, None),
                          ("text-chunks", BASE + 1, None),
                          ("assistant/chunk", BASE + 2, None)])
check("纯 chunk 序列 → tail 为空", db.format_tail(only_chunk_ev, None) == [],
      str(db.format_tail(only_chunk_ev, None)))

print("== ③ tool/call 工具名 + 参数摘要 ==")
tool_ev, tool_t = build([
    ("tool/call", BASE, {"name": "read",
                         "arguments": '{"file_path": "C:\\\\x\\\\y.py"}'}),
    ("tool/call", BASE + 1, {"name": "grep", "arguments": "{}"}),
])
tool_lines = db.format_tail(tool_ev, tool_t)
check("bash 摘要为命令", any("tool/call bash: pytest -q" in ln for ln in lines),
      str(lines))
check("read 摘要为文件路径",
      "tool/call read:" in tool_lines[1] and "y.py" in tool_lines[1],
      str(tool_lines))
check("其他工具名保留", "tool/call grep" in tool_lines[0], str(tool_lines))
check("tool/call 行按倒序排列（grep 在上、read 在下）",
      "tool/call grep" in tool_lines[0] and "tool/call read" in tool_lines[1],
      str(tool_lines))

print("== ④ todo/write 当前第 k 项/共 n 项 ==")
check("有 status → 第一个未完成项",
      any("todo: 当前第 2 项/共 3 项" in ln for ln in lines), str(lines))
no_status_ev, no_status_t = build([
    ("todo/write", BASE, {"todos": [{"content": "x0"}, {"content": "x1"},
                                    {"content": "x2"}]}),
    ("todo/write", BASE + 1, {"todos": [{"content": "x0"}, {"content": "x1"},
                                        {"content": "x2"}]}),
])
no_status_lines = db.format_tail(no_status_ev, no_status_t)
check("无 status 第 2 次写入 → 当前第 1 项/共 3 项（倒序：最新在上）",
      "todo: 当前第 1 项/共 3 项" in no_status_lines[0], str(no_status_lines))

print("== ⑦ max_lines 截断（过滤后计数）+ 倒序 ==")
trunc = db.format_tail(events, times, max_lines=2)
check("max_lines=2 → 只保留最近 2 条（倒序）", len(trunc) == 2, str(trunc))
check("最近 2 条为 step/start 与 assistant/message（最新在上）",
      trunc[0].startswith("[22] ") and "step/start" in trunc[0]
      and "assistant/message" in trunc[1], str(trunc))
check("截断后无 time 行仍用全文件序号 [22]", trunc[0].startswith("[22] "), trunc[0])
check("max_lines=0 → 空列表", db.format_tail(events, times, max_lines=0) == [],
      str(db.format_tail(events, times, max_lines=0)))

print("== ⑤ 无 time 列表 → 全部行首为 [序号] ==")
no_t_ev, _ = build([("reasoning-chunks", None, None),
                    ("step/start", None, {"step": 1})])
no_t_lines = db.format_tail(no_t_ev, None)
check("chunk 被过滤后只剩 step/start",
      len(no_t_lines) == 1 and "step/start" in no_t_lines[0], str(no_t_lines))
check("无 time 行首为 [序号]",
      all(re.match(r"^\[\d+\] ", ln) for ln in no_t_lines), str(no_t_lines))

print(f"\n{'=' * 40}\n结果: {'全部通过 ✅' if fails == 0 else f'{fails} 项失败 ❌'}")
sys.exit(1 if fails else 0)
