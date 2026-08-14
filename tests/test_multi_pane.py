# -*- coding: utf-8 -*-
"""dsh-progress-viz 多任务分栏单测（纯本地，不烧 token；只用合成 fixtures）

运行方式：python tests\test_multi_pane.py（无需 pytest），exit 0 全过。
会话 fixture 动态生成到临时目录（不碰 ~/.dsh/sessions 真实会话）。

覆盖（按 spec）：
  ① 最近 1 小时窗口：2 小时前的任务被过滤，3 分钟前的 completed 保留
  ② 按 mtime 降序：running（最新）在前
  ③ status 判定：文件 mtime 距今 ≤ 30s → running，否则 completed
  ④ 字段齐全（id/cwd/status/stage/stage_idx/stage_total/stage_pct/action/
     eta_s/eta_mode/eta_at/elapsed_s/tail）
  ⑤ ETA 独立计算（exclude 自身会话）：单会话目录 → hist 不可用 → linear
  ⑥ 空目录 → tasks=[]
  ⑦ 损坏会话静默跳过（不崩溃）
"""
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)  # publish\dsh-progress-viz
sys.path.insert(0, PKG)   # 使 dashboard.py / session_progress.py 可导入
sys.path.insert(0, HERE)  # 使同目录下的 make_fixtures.py 可导入
import session_progress as sp
import make_fixtures as mf
import dashboard as db

fails = 0


def check(name, cond, detail=""):
    global fails
    if cond:
        print(f"  PASS {name}")
    else:
        fails += 1
        print(f"  FAIL {name} {detail}")


def with_root(fn):
    """把 sp.SESSIONS_ROOT 临时指向新目录执行 fn(root)，结束后恢复并清理。"""
    root = tempfile.mkdtemp(prefix="dsh-multipane-test-")
    old = sp.SESSIONS_ROOT
    sp.SESSIONS_ROOT = root
    try:
        return fn(root)
    finally:
        sp.SESSIONS_ROOT = old
        shutil.rmtree(root, ignore_errors=True)


def session_events(sid, cwd, start_ms, dur_ms=60000):
    """合成一个会话的事件列表（session + step/start×4 + todo/write + tool/call）。

    阶段：todo 清单第一个未完成项 → 「步骤B」（idx=2/total=3）；
    最近动作：bash pytest -q；事件 time 跨度 = dur_ms（首尾差 60s）。
    """
    events = [{"type": "session", "version": 0, "id": sid,
               "createdAt": start_ms, "cwd": cwd, "delegationDepth": 0,
               "time": start_ms}]
    for i in range(1, 5):
        t = start_ms + dur_ms * i // 4
        events.append({"type": "step/start", "seq": i, "time": t,
                       "data": {"turn": 1, "step": i}})
    events.append({"type": "todo/write", "seq": 5, "time": start_ms + dur_ms,
                   "data": {"todos": [
                       {"content": "步骤A", "status": "completed"},
                       {"content": "步骤B", "status": "in_progress"},
                       {"content": "步骤C", "status": "pending"}]}})
    events.append({"type": "tool/call", "seq": 6, "time": start_ms + dur_ms + 1000,
                   "data": {"name": "bash",
                            "arguments": json.dumps({"command": "pytest -q"})}})
    return events


def write_session(root, cwd, sid, age_s, start_ms=None):
    """在 root 下写一个合成会话（zstd 多 frame），并把 mtime 设为 now−age_s。

    布局：<root>/<cwd编码>/<sid>/session.jsonl.zstd（与真实会话目录一致）。
    """
    if start_ms is None:
        start_ms = (time.time() - age_s) * 1000
    cwd_dir = os.path.join(root, sp.encode_cwd(cwd))
    path = os.path.join(cwd_dir, sid, "session.jsonl.zstd")
    mf.write_fixture(path, session_events(sid, cwd, start_ms))
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


print("== 多任务扫描（1 小时窗口 + mtime 降序 + status + 字段）==")
def t1(root):
    now = time.time()
    CWD1 = r"C:\Users\demo\task-a"
    CWD2 = r"C:\Users\demo\task-b"
    CWD3 = r"C:\Users\demo\task-c"
    SID1 = "session-11111111-0000-0000-0000-000000000001"  # ① running（mtime=now）
    SID2 = "session-22222222-0000-0000-0000-000000000002"  # ② 2 小时前（超窗口）
    SID3 = "session-33333333-0000-0000-0000-000000000003"  # ③ 3 分钟前（completed）
    write_session(root, CWD1, SID1, 0, int((now - 30) * 1000))       # ① 30s 前开始
    write_session(root, CWD2, SID2, 7200, int((now - 7260) * 1000))  # ② 2 小时前
    write_session(root, CWD3, SID3, 180, int((now - 240) * 1000))    # ③ 3 分钟前

    tasks = db.scan_tasks()
    ids = [t["id"] for t in tasks]
    check("窗口过滤：只含 ① 和 ③（② 超 1 小时被过滤）",
          ids == ["11111111", "33333333"], str(ids))
    t_run, t_done = tasks[0], tasks[1]
    check("按 mtime 降序：running（最新）在前",
          tasks[0]["id"] == "11111111" and tasks[1]["id"] == "33333333", str(ids))
    check("① status=running", t_run["status"] == "running", str(t_run["status"]))
    check("③ status=completed", t_done["status"] == "completed", str(t_done["status"]))

    need = {"id", "cwd", "status", "stage", "stage_idx", "stage_total",
            "stage_pct", "action", "eta_s", "eta_mode", "eta_at",
            "elapsed_s", "tail"}
    for i, t in enumerate(tasks):
        check(f"任务{i} 字段齐全", need <= set(t.keys()), str(sorted(t.keys())))

    check("① cwd 正确", t_run["cwd"] == CWD1, str(t_run["cwd"]))
    check("① stage 取第一个未完成项（步骤B 2/3）",
          t_run["stage"] == "步骤B" and t_run["stage_idx"] == 2
          and t_run["stage_total"] == 3, str(t_run))
    check("① stage_pct = 67", t_run["stage_pct"] == 67, str(t_run["stage_pct"]))
    check("① action 取最近 tool/call",
          t_run["action"] == "运行 bash 命令: pytest -q", str(t_run["action"]))
    check("① tail 非空（事件流紧凑文本）",
          isinstance(t_run["tail"], list) and len(t_run["tail"]) > 0,
          str(t_run["tail"]))
    check("① elapsed_s ≈ 30s（当前时间 − 首个事件 time）",
          25 <= t_run["elapsed_s"] <= 90, str(t_run["elapsed_s"]))
    check("③ elapsed_s ≈ 240s", 200 <= t_done["elapsed_s"] <= 300,
          str(t_done["elapsed_s"]))
    check("③ 最后阶段名（stage 或 步骤N）", t_done["stage"] == "步骤B",
          str(t_done["stage"]))

    # ETA 独立计算：① 所在目录只有自身 → 历史排除自身后不可用 → 纯 linear
    check("① ETA 独立计算（exclude 自身 → hist=0 → linear）",
          t_run["eta_mode"] == "linear" and isinstance(t_run["eta_s"], (int, float)),
          f"{t_run['eta_mode']} {t_run['eta_s']}")
    check("① eta_at 存在", isinstance(t_run["eta_at"], str)
          and len(t_run["eta_at"]) > 0, str(t_run["eta_at"]))
with_root(t1)

print("== 空目录 → tasks=[] ==")
def t2(root):
    tasks = db.scan_tasks()
    check("空目录 tasks=[]", tasks == [], str(tasks))
with_root(t2)

print("== 损坏会话静默跳过（不崩溃）==")
def t3(root):
    cwd_dir = os.path.join(root, sp.encode_cwd(r"C:\Users\demo\bad"))
    path = os.path.join(cwd_dir, "session-bad-0000", "session.jsonl.zstd")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"this is not zstd data at all")
    os.utime(path, (time.time(), time.time()))
    tasks = db.scan_tasks()
    check("损坏会话被跳过 → tasks=[]", tasks == [], str(tasks))
with_root(t3)

print(f"\n{'=' * 40}\n结果: {'全部通过 ✅' if fails == 0 else f'{fails} 项失败 ❌'}")
sys.exit(1 if fails else 0)
