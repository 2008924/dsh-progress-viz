# -*- coding: utf-8 -*-
"""dsh-progress-viz 发布包单测（纯本地，不烧 token；只用合成 fixtures，无真实会话）
运行方式：python tests\test_session_progress.py（无需 pytest），exit 0 全过。

覆盖（按 spec）：
  - 合成 fixture 完整解压成功（行数与事件类型数量符合标注）
  - build_progress 阶段正确（todo 优先 / 无 todo 回退步骤计数 / 无状态字段规则）
  - format_action 三类规则（bash / read+write / 其他名称）
  - encode_cwd 映射（盘符冒号、分隔符、正斜杠归一化）
  - tail_session 增量（二次调用新行为 0）
  - 坏行容错（parse_event None / 损坏 zstd 静默返回空 / 尾部半行丢弃）
  - 合成 fixture 为多 frame zstd（stream_reader 全量可解，decompressobj 只解首帧）
"""
import json
import os
import sys

import zstandard

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)  # publish\dsh-progress-viz
FIX_DIR = os.path.join(HERE, "fixtures")
FIX = os.path.join(FIX_DIR, "session-synthetic.jsonl.zstd")

sys.path.insert(0, PKG)   # 使 tests\ 同级目录下的 session_progress.py 可导入
sys.path.insert(0, HERE)  # 使同目录下的 make_fixtures.py 可导入
import session_progress as sp
import make_fixtures as mf

# spec 标注的事件数量（生成器与测试共用同一份约定）
EXPECTED_COUNTS = {"session": 1, "todo/write": 3, "step/start": 8,
                   "tool/call": 6, "assistant/message": 5}

fails = 0


def check(name, cond, detail=""):
    global fails
    if cond:
        print(f"  PASS {name}")
    else:
        fails += 1
        print(f"  FAIL {name} {detail}")


def count_types(events):
    """事件列表 → 类型计数 dict（兼容事件 dict 与 parse_event 二元组）。"""
    c = {}
    for ev in events:
        typ = ev[0] if isinstance(ev, tuple) else ev.get("type")
        c[typ] = c.get(typ, 0) + 1
    return c


def to_events(lines):
    return [e for e in (sp.parse_event(l) for l in lines) if e]


if not os.path.isfile(FIX):
    print("ERROR: 合成 fixture 缺失，请先运行 python tests\\make_fixtures.py")
    sys.exit(1)

print("== 生成器事件列表（make_fixtures.generate_events）==")
gen_events = mf.generate_events()
gc = count_types(gen_events)
check("事件总数 == 23", len(gen_events) == 23, f"got {len(gen_events)}")
for typ, n in EXPECTED_COUNTS.items():
    check(f"{typ} == {n}", gc.get(typ, 0) == n, f"got {gc.get(typ, 0)}")

print("== 合成 fixture 完整解压 ==")
lines, total = sp.tail_session(FIX, 0)
check("解压总行数 == 23", total == 23, f"got {total}")
check("全量解压新行数 == 总行数", len(lines) == total, f"{len(lines)}/{total}")

print("== 多 frame zstd 校验（stream_reader 全量 vs decompressobj 首帧）==")
with open(FIX, "rb") as f:
    raw = f.read()
with open(FIX, "rb") as f:
    full_bytes = zstandard.ZstdDecompressor().stream_reader(f).read()
dobj = zstandard.ZstdDecompressor().decompressobj().decompress(raw)
check("stream_reader 全量解压成功", len(full_bytes) > 0, str(len(full_bytes)))
check("decompressobj 只解首帧（fixture 确为多 frame 追加写入）",
      len(dobj) < len(full_bytes), f"{len(dobj)} < {len(full_bytes)}")

print("== 事件类型数量（与 spec 标注一致）==")
c = count_types(to_events(lines))
for typ, n in EXPECTED_COUNTS.items():
    check(f"fixture {typ} == {n}", c.get(typ, 0) == n, f"got {c.get(typ, 0)}")

print("== build_progress（合成 fixture）==")
p = sp.build_progress(to_events(lines))
check("返回合法 dict（含 4 键）", isinstance(p, dict)
      and {"stage", "stage_idx", "stage_total", "action"} <= set(p.keys()), str(p))
check("todo 优先：取第一个未完成项", p.get("stage") == "编写并运行测试"
      and p.get("stage_idx") == 3 and p.get("stage_total") == 3, str(p))
check("action 取最近 tool/call", p.get("action") == "运行 bash 命令: git status",
      str(p.get("action")))

print("== build_progress 规则（合成用例）==")
syn = [("todo/write", {"todos": [
    {"content": "A", "status": "completed"},
    {"content": "B", "status": "in_progress"},
    {"content": "C", "status": "pending"}]})]
ps = sp.build_progress(syn)
check("取第一个未完成项", ps.get("stage") == "B" and ps.get("stage_idx") == 2
      and ps.get("stage_total") == 3, str(ps))
syn_nostatus = [("todo/write", {"todos": [{"content": "x%d" % i} for i in range(5)]})] * 3
pn = sp.build_progress(syn_nostatus)
check("无状态字段 → 第(已写次数-1)项", pn.get("stage_idx") == 2 and pn.get("stage_total") == 5,
      str(pn))
steps = [("step/start", {"turn": 1, "step": i}) for i in range(1, 7)]
pb = sp.build_progress(steps)
check("无 todo 回退步骤计数", pb.get("stage") == "步骤6" and pb.get("stage_idx") == 6
      and pb.get("stage_total") == 0, str(pb))
p0 = sp.build_progress([])
check("空事件流 → stage None", p0.get("stage") is None and p0.get("stage_idx") == 0, str(p0))

print("== encode_cwd ==")
check("绝对路径映射", sp.encode_cwd(r"C:\Users\demo\sample-project")
      == "--C-Users-demo-sample-project--",
      sp.encode_cwd(r"C:\Users\demo\sample-project"))
check("子目录映射", sp.encode_cwd(r"C:\Users\demo\sample-project\tests")
      == "--C-Users-demo-sample-project-tests--",
      sp.encode_cwd(r"C:\Users\demo\sample-project\tests"))
check("正斜杠归一化", sp.encode_cwd("C:/Users/demo/sample-project")
      == "--C-Users-demo-sample-project--",
      sp.encode_cwd("C:/Users/demo/sample-project"))

print("== tail_session 增量 ==")
new2, total2 = sp.tail_session(FIX, total)
check("第二次调用（seen=总行数）新行为 0", new2 == [] and total2 == total, f"{len(new2)} {total2}")

print("== 坏行容错 ==")
check("非 JSON 行 → None", sp.parse_event("not a json line") is None)
check("无 type 的 JSON → None", sp.parse_event('{"a": 1}') is None)
check("None 输入 → None", sp.parse_event(None) is None)
bad = os.path.join(FIX_DIR, "_bad.zstd.tmp")
with open(bad, "wb") as f:
    f.write(b"this is not zstd data at all")
nb, tb = sp.tail_session(bad, 0)
check("损坏 zstd 静默返回空", nb == [] and tb == 0, f"{nb} {tb}")
os.remove(bad)
# 尾部半行（无换行结尾）丢弃：拼接 1 行完整 + 半行 → 只解出 1 行
half = os.path.join(FIX_DIR, "_half.zstd.tmp")
with open(half, "wb") as f:
    f.write(zstandard.ZstdCompressor().compress(
        '{"type":"session","id":"s1"}\n{"type":"step/start"'.encode("utf-8")))
hlines, htotal = sp.tail_session(half, 0)
check("尾部半行丢弃", htotal == 1 and len(hlines) == 1, f"{htotal} {hlines}")
os.remove(half)

print("== format_action 三类规则 ==")
check("bash 取命令", sp.format_action({"name": "bash",
      "arguments": '{"command": "pytest -q"}'}) == "运行 bash 命令: pytest -q",
      str(sp.format_action({"name": "bash", "arguments": '{"command": "pytest -q"}'})))
check("read 取文件名", sp.format_action({"name": "read",
      "arguments": r'{"file_path": "C:\\x\\y.py"}'}) == "读取文件: y.py",
      str(sp.format_action({"name": "read", "arguments": r'{"file_path": "C:\\x\\y.py"}'})))
check("write 取文件名", sp.format_action({"name": "write",
      "arguments": r'{"file_path": "C:\\x\\notes.md"}'}) == "写入文件: notes.md",
      str(sp.format_action({"name": "write", "arguments": r'{"file_path": "C:\\x\\notes.md"}'})))
check("其他取名称", sp.format_action({"name": "grep", "arguments": "{}"}) == "grep",
      str(sp.format_action({"name": "grep", "arguments": "{}"})))
check("非法输入 → None", sp.format_action(None) is None)

print(f"\n{'=' * 40}\n结果: {'全部通过 ✅' if fails == 0 else f'{fails} 项失败 ❌'}")
sys.exit(1 if fails else 0)
