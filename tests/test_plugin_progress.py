# -*- coding: utf-8 -*-
"""dsh-progress-viz 插件版数据源单测（纯本地，不烧 token；只用合成 fixtures）

运行方式：python tests\test_plugin_progress.py（无需 pytest），exit 0 全过。
插件输出 JSON（<DSH_HOME>/progress/*.json）动态生成到临时目录，
不碰 ~/.dsh/progress 真实目录。

覆盖（按 spec）：
  ① read_progress_json：读取插件 JSON → 任务字段与 /api/live 对齐
     （id/cwd/title/stage/stage_idx/stage_total/stage_pct/action/cost_est/
      elapsed_s/status/timeline/tail；eta 无插件数据 → None）
  ② 优先级：插件数据优先（zstd 会话与插件 JSON 同时存在 → 取插件任务）
  ③ 回退：无插件文件（目录缺失/为空）→ 回退 zstd 解析（scan_tasks 不变）
  ④ status：finished=true → completed；未结束 + mtime 新 → running
  ⑤ current.json 排除（按 session_id 去重，避免与 per-session 文件重复）
  ⑥ 窗口/上限：>1 小时旧文件过滤、MAX_TASKS 截断、损坏 JSON 静默跳过
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


def with_progress_root(fn):
    """把 db.PROGRESS_ROOT 与 sp.SESSIONS_ROOT 临时指向新目录执行 fn(root)。

    插件 JSON 与 zstd 会话都写到同一临时 root（不同子路径），
    结束后恢复并清理（不碰真实 ~/.dsh 数据）。
    """
    root = tempfile.mkdtemp(prefix="dsh-plugin-progress-test-")
    old_root = db.PROGRESS_ROOT
    old_sessions = sp.SESSIONS_ROOT
    db.PROGRESS_ROOT = root            # 插件 JSON 平铺在 root 下
    sp.SESSIONS_ROOT = root            # zstd 会话在 root/<cwd编码>/... 子目录
    try:
        return fn(root)
    finally:
        db.PROGRESS_ROOT = old_root
        sp.SESSIONS_ROOT = old_sessions
        shutil.rmtree(root, ignore_errors=True)


def plugin_json(sid, cwd, title="插件版任务标题", stage="实现核心逻辑",
                stage_idx=2, stage_total=3, action="运行 bash 命令: pytest -q",
                cost_est=0.012345, elapsed_s=42, finished=False,
                timeline=None, updated_at=None):
    """构造插件输出格式的进度 JSON dict（字段与插件 README 对齐）。"""
    if timeline is None:
        timeline = [
            {"t": "12:00:01", "type": "todo/write", "desc": "当前第 2 项/共 3 项"},
            {"t": "12:00:05", "type": "tool/call", "desc": "运行 bash 命令: pytest -q"},
        ]
    if updated_at is None:
        updated_at = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    return {
        "session_id": sid,
        "title": title,
        "cwd": cwd,
        "stage": stage,
        "stage_idx": stage_idx,
        "stage_total": stage_total,
        "stage_pct": round(stage_idx / stage_total * 100) if stage_total else 0,
        "action": action,
        "cost_est": cost_est,
        "elapsed_s": elapsed_s,
        "updated_at": updated_at,
        "finished": finished,
        "timeline": timeline,
    }


def write_progress(root, data, name=None, age_s=0):
    """把插件 JSON 写入 root（默认按 session_id 命名），mtime 设为 now−age_s。"""
    os.makedirs(root, exist_ok=True)
    if name is None:
        name = data["session_id"].replace("session-", "") + ".json"
    path = os.path.join(root, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


def write_zstd_session(root, cwd, sid, age_s=20):
    """在 root 下写一个合成 zstd 会话（与现有测试同款），返回文件路径。"""
    start_ms = int((time.time() - age_s - 30) * 1000)
    cwd_dir = os.path.join(root, sp.encode_cwd(cwd))
    path = os.path.join(cwd_dir, sid, "session.jsonl.zstd")
    events = [{"type": "session", "version": 0, "id": sid,
               "createdAt": start_ms, "cwd": cwd, "delegationDepth": 0,
               "time": start_ms}]
    for i in range(1, 4):
        t = start_ms + i * 1000
        events.append({"type": "step/start", "seq": i, "time": t,
                       "data": {"turn": 1, "step": i}})
    events.append({"type": "todo/write", "seq": 4, "time": start_ms + 4000,
                   "data": {"todos": [
                       {"content": "步骤A", "status": "completed"},
                       {"content": "步骤B", "status": "in_progress"},
                       {"content": "步骤C", "status": "pending"}]}})
    mf.write_fixture(path, events)
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


print("== ① read_progress_json 读取插件 JSON → 任务字段对齐 ==")
def t1(root):
    SID = "session-aaaaaaaa-0000-0000-0000-000000000001"
    CWD = r"C:\Users\demo\plugin-task"
    write_progress(root, plugin_json(SID, CWD, finished=False), age_s=0)
    tasks = db.read_progress_json()
    check("读到 1 个任务", len(tasks) == 1, str(len(tasks)))
    t = tasks[0] if tasks else {}
    need = {"id", "cwd", "title", "status", "stage", "stage_idx", "stage_total",
            "stage_pct", "action", "cost_est", "elapsed_s", "timeline", "tail"}
    check("任务字段齐全", need <= set(t.keys()), str(sorted(t.keys())))
    check("id = session_id 去前缀前 8 位", t.get("id") == "aaaaaaaa", str(t.get("id")))
    check("title 透传插件标题", t.get("title") == "插件版任务标题", str(t.get("title")))
    check("cwd 透传", t.get("cwd") == CWD, str(t.get("cwd")))
    check("stage/stage_idx/stage_total 透传",
          t.get("stage") == "实现核心逻辑" and t.get("stage_idx") == 2
          and t.get("stage_total") == 3 and t.get("stage_pct") == 67,
          str({k: t.get(k) for k in ("stage", "stage_idx", "stage_total", "stage_pct")}))
    check("action 透传（已过滤噪音的中文摘要）",
          t.get("action") == "运行 bash 命令: pytest -q", str(t.get("action")))
    check("cost_est 透传", t.get("cost_est") == 0.012345, str(t.get("cost_est")))
    check("elapsed_s 为 int", isinstance(t.get("elapsed_s"), int), str(t.get("elapsed_s")))
    check("timeline 透传（≤50 条）",
          isinstance(t.get("timeline"), list) and len(t.get("timeline")) == 2,
          str(t.get("timeline")))
    check("tail 由 timeline 派生且非空",
          isinstance(t.get("tail"), list) and len(t.get("tail")) > 0,
          str(t.get("tail")))
    check("eta 无插件数据 → None",
          t.get("eta_s") is None and t.get("eta_mode") is None
          and t.get("eta_at") is None, str({k: t.get(k) for k in ("eta_s", "eta_mode", "eta_at")}))
    check("未结束 + mtime 新 → running", t.get("status") == "running", str(t.get("status")))
with_progress_root(t1)

print("== ② 优先级：插件数据优先（zstd 与插件 JSON 同时存在）==")
def t2(root):
    db._PARSE_CACHE.clear()
    CWD = r"C:\Users\demo\plugin-priority"
    SID_PLUGIN = "session-bbbbbbbb-0000-0000-0000-000000000002"
    SID_ZSTD = "session-cccccccc-0000-0000-0000-000000000003"
    write_zstd_session(root, CWD, SID_ZSTD, age_s=20)     # zstd 会话存在
    write_progress(root, plugin_json(SID_PLUGIN, CWD, title="插件数据优先"),
                   age_s=0)                                # 插件 JSON 也存在
    tasks = db.scan_tasks()
    check("scan_tasks 返回插件任务（不返回 zstd 任务）",
          len(tasks) == 1 and tasks[0]["id"] == "bbbbbbbb"
          and tasks[0]["title"] == "插件数据优先",
          str([(t.get("id"), t.get("title")) for t in tasks]))
with_progress_root(t2)

print("== ③ 回退：无插件文件 → 回退 zstd 解析 ==")
def t3(root):
    db._PARSE_CACHE.clear()
    CWD = r"C:\Users\demo\plugin-fallback"
    SID = "session-dddddddd-0000-0000-0000-000000000004"
    check("progress 目录不存在 → read_progress_json []",
          db.read_progress_json() == [], str(db.read_progress_json()))
    write_zstd_session(root, CWD, SID, age_s=20)  # 只有 zstd 会话
    tasks = db.scan_tasks()
    check("回退 zstd 解析（任务来自 zstd）",
          len(tasks) == 1 and tasks[0]["id"] == "dddddddd",
          str([(t.get("id"), t.get("title")) for t in tasks]))
    check("回退任务字段语义不变（stage 来自 todo 清单）",
          tasks and tasks[0]["stage"] == "步骤B" and tasks[0]["stage_idx"] == 2
          and tasks[0]["stage_total"] == 3,
          str(tasks[0] if tasks else None))
with_progress_root(t3)

print("== ④ status：finished=true → completed ==")
def t4(root):
    SID = "session-eeeeeeee-0000-0000-0000-000000000005"
    CWD = r"C:\Users\demo\plugin-done"
    write_progress(root, plugin_json(SID, CWD, finished=True), age_s=0)
    tasks = db.read_progress_json()
    check("finished=true（即使 mtime 新）→ completed",
          tasks and tasks[0]["status"] == "completed", str(tasks))
    # 未结束 + mtime 变旧 → completed（沿用 RUNNING_AGE 语义）
    SID2 = "session-ffffffff-0000-0000-0000-000000000006"
    write_progress(root, plugin_json(SID2, CWD, finished=False), age_s=300)
    tasks2 = db.read_progress_json()
    by_id = {t["id"]: t for t in tasks2}
    check("未结束 + mtime 旧 → completed",
          by_id.get("ffffffff", {}).get("status") == "completed",
          str({k: v.get("status") for k, v in by_id.items()}))
with_progress_root(t4)

print("== ⑤ current.json 排除（session_id 去重）==")
def t5(root):
    SID = "session-11111111-0000-0000-0000-000000000007"
    CWD = r"C:\Users\demo\plugin-current"
    write_progress(root, plugin_json(SID, CWD), name="current.json", age_s=0)
    write_progress(root, plugin_json(SID, CWD), age_s=0)  # 同名 per-session 文件
    tasks = db.read_progress_json()
    check("current.json 与 per-session 文件只算一个任务",
          len(tasks) == 1, str(len(tasks)))
with_progress_root(t5)

print("== ⑥ 窗口/上限/损坏容错 ==")
def t6(root):
    SID = "session-22222222-0000-0000-0000-000000000008"
    CWD = r"C:\Users\demo\plugin-window"
    write_progress(root, plugin_json(SID, CWD), age_s=7200)  # 2 小时前 → 过滤
    check(">1 小时旧文件被过滤",
          db.read_progress_json() == [], str(db.read_progress_json()))
    # 损坏 JSON 静默跳过
    with open(os.path.join(root, "broken.json"), "w", encoding="utf-8") as f:
        f.write("{not-json")
    t_broken = time.time() - 10
    os.utime(os.path.join(root, "broken.json"), (t_broken, t_broken))
    SID2 = "session-33333333-0000-0000-0000-000000000009"
    write_progress(root, plugin_json(SID2, CWD), age_s=5)
    tasks = db.read_progress_json()
    check("损坏 JSON 静默跳过（不崩溃）",
          len(tasks) == 1 and tasks[0]["id"] == "33333333", str(len(tasks)))
    # MAX_TASKS 截断（按 mtime 降序取前 N）
    old_max = db.MAX_TASKS
    db.MAX_TASKS = 2
    try:
        for i in range(4):
            write_progress(root, plugin_json(
                "session-%08d-0000-0000-0000-00000000000%d" % (40000000 + i, i),
                CWD), age_s=i)  # 0/1/2/3 秒前 → 最新两个是 40000000/40000001
        tasks3 = db.read_progress_json()
        check("超上限 → 只保留最近 MAX_TASKS 个",
              len(tasks3) == 2 and tasks3[0]["id"] == "40000000"
              and tasks3[1]["id"] == "40000001",
              str([t["id"] for t in tasks3]))
    finally:
        db.MAX_TASKS = old_max
with_progress_root(t6)

print("== ⑦ /api/live 字段语义不变（live_payload 走插件任务）==")
def t7(root):
    SID = "session-55555555-0000-0000-0000-000000000010"
    CWD = r"C:\Users\demo\plugin-live"
    write_progress(root, plugin_json(SID, CWD, finished=False), age_s=0)
    with db._LOCK:
        db._STATE["tasks"] = db.read_progress_json()
    try:
        payload = db.live_payload()
        check("live_payload 返回 tasks 列表", "tasks" in payload,
              str(list(payload.keys())))
        check("live_payload 不含私有键 _sid",
              len(payload["tasks"]) == 1 and all("_sid" not in t
                                                 for t in payload["tasks"]),
              str(payload["tasks"]))
        check("插件任务字段语义与 zstd 任务一致",
              payload["tasks"] and "status" in payload["tasks"][0]
              and "stage" in payload["tasks"][0] and "elapsed_s" in payload["tasks"][0],
              str(payload["tasks"][0].keys() if payload["tasks"] else None))
    finally:
        with db._LOCK:
            db._STATE["tasks"] = []
with_progress_root(t7)

print(f"\n{'=' * 40}\n结果: {'全部通过 ✅' if fails == 0 else f'{fails} 项失败 ❌'}")
sys.exit(1 if fails else 0)
