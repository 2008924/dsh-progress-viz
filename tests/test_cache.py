# -*- coding: utf-8 -*-
"""dsh-progress-viz 解析缓存单测（纯本地，不烧 token；只用合成 fixtures）

运行方式：python tests\test_cache.py（无需 pytest），exit 0 全过。
会话 fixture 动态生成到临时目录（不碰 ~/.dsh/sessions 真实会话）。

覆盖（按 spec）：
  ① 首扫对每个新会话文件解压解析（解压计数 = 任务数）
  ② 二次扫描：文件 mtime 未变 → 不重新解压（解压计数为 0，缓存复用）
  ③ 解析字段与首扫一致（id/cwd/stage/stage_idx/stage_total/stage_pct/
     action/eta_s/eta_mode），仅依赖 now 的 status/elapsed_s/eta_at 照常刷新
  ④ 修改 mtime → 对应文件重新解压解析（计数 +1，且只重解析被改的那个）
  ⑤ 新增文件 → 只解析新文件（计数 +1，旧文件仍走缓存）
  ⑥ 会话文件被删除 → 缓存条目随之失效（_PARSE_CACHE 无残留）
  ⑦ cwd 目录消失 → 缓存全部清理、tasks=[]
  ⑧ 任务字段与 /api/live 约定一致（13 字段）
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
    """把 sp.SESSIONS_ROOT / db.PROGRESS_ROOT 临时指向新目录执行 fn(root)，结束后恢复并清理。"""
    root = tempfile.mkdtemp(prefix="dsh-cache-test-")
    old = sp.SESSIONS_ROOT
    old_prog = db.PROGRESS_ROOT
    sp.SESSIONS_ROOT = root
    db.PROGRESS_ROOT = os.path.join(root, "progress")  # 不存在 → scan_tasks 回退 zstd 解析
    try:
        return fn(root)
    finally:
        sp.SESSIONS_ROOT = old
        db.PROGRESS_ROOT = old_prog
        shutil.rmtree(root, ignore_errors=True)


def session_events(sid, cwd, start_ms):
    """合成一个会话的事件列表（session + step/start×3 + todo/write + tool/call）。

    阶段：todo 清单第一个未完成项 → 「步骤B」（idx=2/total=3）；
    最近动作：bash pytest -q；事件 time 从 start_ms 起递增（跨度 6s）。
    """
    events = [{"type": "session", "version": 0, "id": sid,
               "createdAt": start_ms, "cwd": cwd, "delegationDepth": 0,
               "time": start_ms}]
    for i in range(1, 4):
        events.append({"type": "step/start", "seq": i, "time": start_ms + i * 1000,
                       "data": {"turn": 1, "step": i}})
    events.append({"type": "todo/write", "seq": 4, "time": start_ms + 5000,
                   "data": {"todos": [
                       {"content": "步骤A", "status": "completed"},
                       {"content": "步骤B", "status": "in_progress"},
                       {"content": "步骤C", "status": "pending"}]}})
    events.append({"type": "tool/call", "seq": 5, "time": start_ms + 6000,
                   "data": {"name": "bash",
                            "arguments": json.dumps({"command": "pytest -q"})}})
    return events


def write_session(root, cwd, sid, age_s):
    """在 root 下写一个合成会话（zstd），并把文件 mtime 设为 now−age_s。

    布局：<root>/<cwd编码>/<sid>/session.jsonl.zstd（与真实会话目录一致）；
    每个会话用独立的 cwd 目录 → compute_hist_s 扫描时不触碰对方文件，
    解压计数只反映任务自身的解析行为。
    """
    cwd_dir = os.path.join(root, sp.encode_cwd(cwd))
    path = os.path.join(cwd_dir, sid, "session.jsonl.zstd")
    mf.write_fixture(path, session_events(sid, cwd,
                                          int((time.time() - age_s) * 1000)))
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


print("== ① 首扫：每个新会话文件都解压解析 ==")
def t1(root):
    CWD1 = r"C:\Users\demo\cache-a"
    CWD2 = r"C:\Users\demo\cache-b"
    CWD3 = r"C:\Users\demo\cache-c"
    SID1 = "session-aaaaaaaa-0000-0000-0000-000000000001"
    SID2 = "session-bbbbbbbb-0000-0000-0000-000000000002"
    SID3 = "session-cccccccc-0000-0000-0000-000000000003"
    p1 = write_session(root, CWD1, SID1, 20)
    p2 = write_session(root, CWD2, SID2, 20)

    db._PARSE_CACHE.clear()   # 测试内隔离（正常流程由 _prune_parse_cache 维护）
    count["n"] = 0
    tasks = db.scan_tasks()
    check("首扫 2 个任务", len(tasks) == 2, str(len(tasks)))
    check("首扫解压 2 次（每任务 1 次）", count["n"] == 2, f"n={count['n']}")
    ids = sorted(t["id"] for t in tasks)
    check("任务 id 正确", ids == ["aaaaaaaa", "bbbbbbbb"], str(ids))
    cached = all(p in db._PARSE_CACHE for p in (p1, p2))
    check("缓存填充两个会话", cached, str(sorted(db._PARSE_CACHE.keys())))
    mt_ok = all(abs(db._PARSE_CACHE[p][0] - os.path.getmtime(p)) < 1e-9
                for p in (p1, p2))
    check("缓存 mtime 与文件一致", mt_ok, str(db._PARSE_CACHE))

    print("== ② 二次扫描：未变化文件不重新解压 ==")
    count["n"] = 0
    tasks2 = db.scan_tasks()
    check("二次扫描解压 0 次（缓存复用）", count["n"] == 0, f"n={count['n']}")
    check("二次扫描任务数不变", len(tasks2) == 2, str(len(tasks2)))

    print("== ③ 解析字段与首扫一致（now 相关字段照常刷新）==")
    for a, b in zip(sorted(tasks, key=lambda t: t["id"]),
                    sorted(tasks2, key=lambda t: t["id"])):
        check("解析字段复用一致",
              (a["id"], a["cwd"], a["stage"], a["stage_idx"],
               a["stage_total"], a["stage_pct"], a["action"],
               a["eta_s"], a["eta_mode"]) ==
              (b["id"], b["cwd"], b["stage"], b["stage_idx"],
               b["stage_total"], b["stage_pct"], b["action"],
               b["eta_s"], b["eta_mode"]),
              str((a, b)))
    check("二次扫描 status 仍正确（running）",
          all(t["status"] == "running" for t in tasks2), str(tasks2))

    print("== ④ 修改 mtime → 对应文件重新解析 ==")
    t_new = time.time() - 5
    os.utime(p1, (t_new, t_new))
    count["n"] = 0
    tasks3 = db.scan_tasks()
    check("mtime 修改后重新解析（解压 1 次，只重解析被改的）",
          count["n"] == 1, f"n={count['n']}")
    check("mtime 修改后缓存 mtime 已更新",
          abs(db._PARSE_CACHE[p1][0] - t_new) < 1e-6, str(db._PARSE_CACHE[p1][0]))
    check("未修改文件仍走缓存（p2 仍在缓存）", p2 in db._PARSE_CACHE,
          str(db._PARSE_CACHE.keys()))

    print("== ⑤ 新增文件 → 只解析新文件 ==")
    p3 = write_session(root, CWD3, SID3, 10)
    count["n"] = 0
    tasks4 = db.scan_tasks()
    check("新增文件后任务数 3", len(tasks4) == 3, str(len(tasks4)))
    check("新文件解压 1 次（旧文件走缓存）", count["n"] == 1, f"n={count['n']}")
    check("新文件已入缓存", p3 in db._PARSE_CACHE, str(db._PARSE_CACHE.keys()))

    print("== ⑥ 会话文件被删除 → 缓存条目随之失效 ==")
    os.remove(p2)
    count["n"] = 0
    tasks5 = db.scan_tasks()
    check("删除会话后任务数 2", len(tasks5) == 2, str(len(tasks5)))
    check("删除会话后不触发解压（其余走缓存）", count["n"] == 0, f"n={count['n']}")
    check("删除会话后缓存条目随之失效（无 p2）",
          p2 not in db._PARSE_CACHE, str(db._PARSE_CACHE.keys()))
    check("剩余会话缓存仍在", p1 in db._PARSE_CACHE and p3 in db._PARSE_CACHE,
          str(db._PARSE_CACHE.keys()))

    print("== ⑦ cwd 目录消失 → 缓存条目随之失效、tasks=[] ==")
    shutil.rmtree(os.path.dirname(os.path.dirname(p1)))  # 删除 p1 的 cwd 编码目录
    shutil.rmtree(os.path.dirname(os.path.dirname(p3)))  # 删除 p3 的 cwd 编码目录
    tasks6 = db.scan_tasks()
    check("目录消失 → tasks=[]", tasks6 == [], str(tasks6))
    check("目录消失 → 缓存条目全部清理", db._PARSE_CACHE == {},
          str(db._PARSE_CACHE))

    print("== ⑧ 任务字段与 /api/live 约定一致 ==")
    need = {"id", "cwd", "status", "stage", "stage_idx", "stage_total",
            "stage_pct", "action", "eta_s", "eta_mode", "eta_at",
            "elapsed_s", "tail"}
    check("任务字段齐全（13 字段）",
          need <= set(tasks4[0].keys()), str(sorted(tasks4[0].keys())))


# 解压计数：包装 sp._read_zstd（tail_session 的唯一解压入口），统计调用次数
count = {"n": 0}
real_read_zstd = sp._read_zstd
def counting_read_zstd(path):
    count["n"] += 1
    return real_read_zstd(path)
sp._read_zstd = counting_read_zstd
try:
    with_root(t1)
finally:
    sp._read_zstd = real_read_zstd

print(f"\n{'=' * 40}\n结果: {'全部通过 ✅' if fails == 0 else f'{fails} 项失败 ❌'}")
sys.exit(1 if fails else 0)
