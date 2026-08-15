# -*- coding: utf-8 -*-
"""dsh-progress-viz ETA 融合算法单测（纯本地，不烧 token；只用合成 fixtures）

运行方式：python tests\test_eta_blend.py（无需 pytest），exit 0 全过。
历史会话 fixture 动态生成到临时目录（不碰 ~/.dsh/sessions 真实会话）。

覆盖（按 spec）：
  ① 历史会话耗时计算正确（事件流首尾 time 差）
  ② 中位数逻辑（奇数/偶数个历史会话）与 hist_s 端到端取值（60s/120s → 90s）
  ③ 融合公式：k=4 → α=0.7 → eta=88s；k=2 → α=0.5 → eta=80s；k=1 → 纯历史 60s
  ④ 回退链：有阶段无历史 → linear；无阶段有历史 → history；都无 → none
  ⑤ eta_mode 正确标记 blend/linear/history/none
  ⑥ 排除当前监控会话 + 历史扫描缓存（目录 mtime 变化才重扫）
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)  # publish\dsh-progress-viz
sys.path.insert(0, PKG)   # 使 dashboard.py / session_progress.py 可导入
sys.path.insert(0, HERE)  # 使同目录下的 make_fixtures.py 可导入
import session_progress as sp
import make_fixtures as mf
import dashboard as db

fails = 0

BASE = 1000000.0  # 固定时间戳（秒）：使 blend_eta 的线性外推完全确定（不依赖 time.time）


def check(name, cond, detail=""):
    global fails
    if cond:
        print(f"  PASS {name}")
    else:
        fails += 1
        print(f"  FAIL {name} {detail}")


def with_root(fn):
    """把 sp.SESSIONS_ROOT 临时指向新目录执行 fn(root)，结束后恢复并清理。"""
    root = tempfile.mkdtemp(prefix="dsh-eta-test-")
    old = sp.SESSIONS_ROOT
    sp.SESSIONS_ROOT = root
    try:
        return fn(root)
    finally:
        sp.SESSIONS_ROOT = old
        shutil.rmtree(root, ignore_errors=True)


def state(k, n, span):
    """构造 blend_eta 的 state：marks[0]=起点，marks[1]=起点+span（线性外推确定）。"""
    return {"started_at": BASE, "stage_marks": [BASE, BASE + span],
            "stage_idx": k, "stage_total": n}


print("== ① 历史会话耗时计算（首尾 time 差）==")
def t1(root):
    paths = mf.write_hist_fixtures(root, durations=(60, 120))
    d0, d1 = db._session_duration(paths[0]), db._session_duration(paths[1])
    check("60s 历史会话耗时 = 60.0", d0 is not None and abs(d0 - 60.0) < 1e-9, str(d0))
    check("120s 历史会话耗时 = 120.0", d1 is not None and abs(d1 - 120.0) < 1e-9, str(d1))
with_root(t1)

print("== ①b 毫秒时间戳单位换算（time 字段是毫秒 → hist_s 必须转秒）==")
def t1b(root):
    # 构造毫秒时间戳会话：首尾 time 差 = 21552ms（≈21.5 秒）——复现历史 bug：
    # 若把「毫秒时间戳差」当成「秒」，hist_s 会变成 21552（ETA 显示 5 小时 59 分）。
    start_ms = 1787000000000
    sid = "session-ms-unit-0000"
    evs = [{"type": "session", "version": 0, "id": sid,
            "createdAt": start_ms, "cwd": mf.CWD, "delegationDepth": 0,
            "time": start_ms}]
    for i in range(1, 4):
        evs.append({"type": "step/start", "seq": i,
                    "time": start_ms + 21552 * i // 3,
                    "data": {"turn": 1, "step": i}})
    path = os.path.join(root, sp.encode_cwd(mf.CWD), sid, "session.jsonl.zstd")
    mf.write_fixture(path, evs)
    dur = db._session_duration(path)
    check("21552ms 首尾差 → 耗时 ≈ 21.552 秒（非 21552）",
          dur is not None and abs(dur - 21.552) < 1e-3, str(dur))
    hist = db.compute_hist_s(mf.CWD, None)
    check("hist_s 正确转秒（21.552，而非 21552）",
          abs(hist - 21.552) < 1e-3, str(hist))
    # 端到端：ETA 融合结果应为秒级（分钟级），而不是 5 小时 59 分
    st = state(2, 3, 21.552 * 0.5)  # k=2 → α=0.5
    eta, mode = db.blend_eta(st, hist)
    check("blend ETA 为秒级（<3600s，修复后不再 6 小时）",
          mode == "blend" and eta is not None and eta < 3600,
          f"eta={eta} mode={mode}")
with_root(t1b)

print("== ② 中位数逻辑（奇数/偶数个历史会话）==")
check("偶数个 [60,120] → 90", db._median([60, 120]) == 90, str(db._median([60, 120])))
check("奇数个 [60,120,180] → 120", db._median([60, 120, 180]) == 120,
      str(db._median([60, 120, 180])))
check("空列表 → 0", db._median([]) == 0, str(db._median([])))

def t2(root):
    paths = mf.write_hist_fixtures(root, durations=(60, 120))  # 2 个 → 偶数
    hist = db.compute_hist_s(mf.CWD, None)
    check("hist_s 端到端 = 90（60s/120s 中位数）", abs(hist - 90.0) < 1e-9, str(hist))
    os.remove(paths[1])  # 剩 1 个 → 奇数
    cwd_dir = os.path.dirname(os.path.dirname(paths[1]))
    t = os.path.getmtime(cwd_dir) + 10  # 目录 mtime 变化 → 触发重扫
    os.utime(cwd_dir, (t, t))
    hist1 = db.compute_hist_s(mf.CWD, None)
    check("hist_s 端到端 = 60（单会话中位数）", abs(hist1 - 60.0) < 1e-9, str(hist1))
with_root(t2)

print("== ⑥ 排除当前监控会话 + 历史扫描缓存 ==")
def t6(root):
    mf.write_hist_fixtures(root, durations=(60, 120))
    # 生成一个「当前会话」：1000s 跨度，扫描时应被排除
    cur_path = os.path.join(root, sp.encode_cwd(mf.CWD),
                            "session-current-0000", "session.jsonl.zstd")
    mf.write_fixture(cur_path, mf.generate_hist_session(1000, sid="session-current-0000"))
    cwd_dir = os.path.dirname(os.path.dirname(cur_path))
    hist = db.compute_hist_s(mf.CWD, cur_path)
    check("排除当前会话后 hist_s = 90", abs(hist - 90.0) < 1e-9, str(hist))
    t = os.path.getmtime(cwd_dir) + 10  # mtime 变化 → 重扫（不排除 → 含 1000s）
    os.utime(cwd_dir, (t, t))
    hist_all = db.compute_hist_s(mf.CWD, None)
    check("不排除时 [60,120,1000] 中位数 = 120", abs(hist_all - 120.0) < 1e-9, str(hist_all))
    hist_again = db.compute_hist_s(mf.CWD, None)  # mtime 未变 → 缓存复用
    check("缓存复用（mtime 未变 → 同值）", hist_again == hist_all, str(hist_again))
with_root(t6)

print("== ③ 融合公式（α 规则）==")
st = state(4, 6, 150.0)   # walked=3, per_stage=50, 剩余 2 → linear=100
eta, mode = db.blend_eta(st, 60.0)
check("k=4: α=0.7 → eta=88, blend", mode == "blend" and abs(eta - 88.0) < 1e-9,
      f"eta={eta} mode={mode}")

st = state(2, 3, 100.0)   # walked=1, per_stage=100, 剩余 1 → linear=100
eta, mode = db.blend_eta(st, 60.0)
check("k=2: α=0.5 → eta=80, blend", mode == "blend" and abs(eta - 80.0) < 1e-9,
      f"eta={eta} mode={mode}")

st = state(1, 5, 0.0)     # k=1 → 无阶段信息（α=0）
eta, mode = db.blend_eta(st, 60.0)
check("k=1: 纯历史 → eta=60, history", mode == "history" and eta == 60.0,
      f"eta={eta} mode={mode}")

print("== ④ 回退链 ==")
eta, mode = db.blend_eta(state(4, 6, 150.0), 0)  # 有阶段信息但无历史
check("有阶段无历史 → 纯 linear=100", mode == "linear" and abs(eta - 100.0) < 1e-9,
      f"eta={eta} mode={mode}")
eta, mode = db.blend_eta(state(1, 5, 0.0), 60.0)  # 无阶段信息但有历史
check("无阶段有历史 → 纯 history=60", mode == "history" and eta == 60.0,
      f"eta={eta} mode={mode}")
eta, mode = db.blend_eta(state(1, 5, 0.0), 0)     # 都无
check("都无 → none / eta=None", mode == "none" and eta is None, f"eta={eta} mode={mode}")

print("== ⑤ eta_mode 标记（四值齐备）==")
modes = sorted([db.blend_eta(state(4, 6, 150.0), 60.0)[1],
                db.blend_eta(state(4, 6, 150.0), 0)[1],
                db.blend_eta(state(1, 5, 0.0), 60.0)[1],
                db.blend_eta(state(1, 5, 0.0), 0)[1]])
check("blend/linear/history/none 齐备", modes == ["blend", "history", "linear", "none"],
      str(modes))

print(f"\n{'=' * 40}\n结果: {'全部通过 ✅' if fails == 0 else f'{fails} 项失败 ❌'}")
sys.exit(1 if fails else 0)
