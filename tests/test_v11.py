# -*- coding: utf-8 -*-
"""dsh-progress-viz v1.1 五项增强单测（纯本地，不烧 token；只用合成 fixtures）

运行方式：python tests\test_v11.py（无需 pytest），exit 0 全过。
会话 fixture 动态生成到临时目录（不碰 ~/.dsh/sessions 真实会话）。

覆盖（按 spec）：
  ① title 解析：session/title 事件 data.title → 任务 title 字段；无标题回退
     cwd basename；再回退完整 cwd
  ② 成本估算：合成 usage 事件（data.usage.inputTokens/outputTokens/…）→
     cost_est 按 PRICES 公式计算正确；无 usage → cost_est=None（禁止硬编假数据）
  ③ CLI --status：status_text 输出含 状态/标题/阶段 k/N/ETA/成本；无任务 → 暂无任务；
     argparse 解析 --status / --feishu-webhook / --no-feishu
  ④ 飞书通知翻转检测：mock webhook，running→completed 触发一次且不重复；
     启动时已 completed 不通知；未启用（--no-feishu / 无 webhook）不发送
  ⑤ timeline 生成：事件 → [{t, type, desc}]，条数 ≤ 50，格式正确
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
    root = tempfile.mkdtemp(prefix="dsh-v11-test-")
    old = sp.SESSIONS_ROOT
    sp.SESSIONS_ROOT = root
    try:
        return fn(root)
    finally:
        sp.SESSIONS_ROOT = old
        shutil.rmtree(root, ignore_errors=True)


def session_events(sid, cwd, start_ms, with_title=True, with_usage=True):
    """合成一个会话的事件列表（session + title + step + todo + usage + tool）。

    title：session/title 事件（data.title）；usage：assistant/message 的
    data.usage（inputTokens/outputTokens/cacheReadTokens，与真实格式一致）。
    """
    events = [{"type": "session", "version": 0, "id": sid,
               "createdAt": start_ms, "cwd": cwd, "delegationDepth": 0,
               "time": start_ms}]
    seq, t = 1, start_ms + 1000
    if with_title:
        events.append({"type": "session/title", "seq": seq, "time": t,
                       "data": {"title": "测试标题：成本显示与飞书通知",
                                "source": {"kind": "provider"}}})
        seq, t = seq + 1, t + 1000
    events.append({"type": "step/start", "seq": seq, "time": t,
                   "data": {"turn": 1, "step": 1}})
    seq, t = seq + 1, t + 1000
    events.append({"type": "todo/write", "seq": seq, "time": t,
                   "data": {"todos": [
                       {"content": "步骤A", "status": "completed"},
                       {"content": "步骤B", "status": "in_progress"},
                       {"content": "步骤C", "status": "pending"}]}})
    seq, t = seq + 1, t + 1000
    if with_usage:
        events.append({"type": "assistant/message", "seq": seq, "time": t,
                       "data": {"turn": 1, "step": 1,
                                "usage": {"inputTokens": 1000, "outputTokens": 500,
                                          "cacheReadTokens": 200, "reasoningTokens": 0},
                                "message": {"role": "assistant", "content": [
                                    {"type": "text", "text": "正在推进"}]}}})
        seq, t = seq + 1, t + 1000
    events.append({"type": "tool/call", "seq": seq, "time": t,
                   "data": {"name": "bash",
                            "arguments": json.dumps({"command": "pytest -q"})}})
    return events


def write_session(root, cwd, sid, age_s, with_title=True, with_usage=True):
    """在 root 下写一个合成会话（zstd），并把文件 mtime 设为 now−age_s。"""
    start_ms = int((time.time() - age_s - 30) * 1000)
    cwd_dir = os.path.join(root, sp.encode_cwd(cwd))
    path = os.path.join(cwd_dir, sid, "session.jsonl.zstd")
    mf.write_fixture(path, session_events(sid, cwd, start_ms,
                                          with_title=with_title,
                                          with_usage=with_usage))
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


def expected_cost(input_tokens, output_tokens, cache_tokens):
    """按 db.PRICES 常量计算期望成本（元），与 estimate_cost 同一公式。"""
    return (input_tokens * db.PRICES["input"]
            + output_tokens * db.PRICES["output"]
            + cache_tokens * db.PRICES.get("cache_read", 0.0)) / 1_000_000.0


print("== ① title 解析（session/title → title 字段；无标题回退）==")
def t1(root):
    CWD = r"C:\Users\demo\v11-title"
    SID = "session-aaaaaaaa-0000-0000-0000-000000000001"
    write_session(root, CWD, SID, 20)  # 有 session/title
    tasks = db.scan_tasks()
    check("有标题 → title 字段 = session/title 的 data.title",
          tasks and tasks[0]["title"] == "测试标题：成本显示与飞书通知",
          str(tasks[0]["title"] if tasks else None))
    # 直接测 _task_title：多 title 事件取最后一个非空
    evs = [("session/title", {"title": "fallback 版"}),
           ("session/title", {"title": "最终版"}),
           ("session/title", {"title": ""})]
    check("多 title 事件取最后一个非空", db._task_title(evs, CWD) == "最终版",
          str(db._task_title(evs, CWD)))
    check("title 为空字符串 → 跳过并回退 cwd basename",
          db._task_title([("session/title", {"title": "  "})], CWD) == "v11-title",
          str(db._task_title([("session/title", {"title": "  "})], CWD)))
with_root(t1)

def t1b(root):
    CWD = r"C:\Users\demo\v11-no-title"
    SID = "session-bbbbbbbb-0000-0000-0000-000000000002"
    write_session(root, CWD, SID, 20, with_title=False)  # 无 session/title
    tasks = db.scan_tasks()
    check("无标题 → 回退 cwd basename", tasks and tasks[0]["title"] == "v11-no-title",
          str(tasks[0]["title"] if tasks else None))
    check("cwd 为 None → title None", db._task_title([], None) is None,
          str(db._task_title([], None)))
with_root(t1b)

print("== ② 成本估算（合成 usage 事件 → cost_est 计算正确；无 usage → None）==")
def t2(root):
    CWD = r"C:\Users\demo\v11-cost"
    SID = "session-cccccccc-0000-0000-0000-000000000003"
    write_session(root, CWD, SID, 20)  # usage: 1000 in / 500 out / 200 cache
    tasks = db.scan_tasks()
    exp = expected_cost(1000, 500, 200)
    check("cost_est 按 PRICES 公式计算正确",
          tasks and tasks[0]["cost_est"] is not None
          and abs(tasks[0]["cost_est"] - exp) < 1e-12,
          f"got={tasks[0]['cost_est'] if tasks else None} exp={exp}")
    check("cost_est 为 float（元）", tasks and isinstance(tasks[0]["cost_est"], float),
          str(tasks and tasks[0]["cost_est"]))
with_root(t2)

# 无 usage → None（禁止硬编假数据）
usage_none = db.scan_usage([("assistant/message", {"message": {"content": "x"}}),
                            ("tool/result", {"result": "ok"}),
                            ("step/start", {"step": 1})])
check("无 usage 事件 → scan_usage None", usage_none is None, str(usage_none))
check("无 usage → estimate_cost None", db.estimate_cost(None) is None,
      str(db.estimate_cost(None)))
check("usage 全 0 → cost_est 0.0", db.estimate_cost(
    {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}) == 0.0,
    str(db.estimate_cost({"input_tokens": 0, "output_tokens": 0,
                          "cache_read_tokens": 0})))

# scan_usage 累计多事件 + snake_case 兼容
usage_evs = [("assistant/message", {"usage": {"inputTokens": 100, "outputTokens": 50}}),
             ("assistant/message", {"usage": {"inputTokens": 200, "outputTokens": 60,
                                              "cacheReadTokens": 30}}),
             ("tool/result", {"usage": {"prompt_tokens": 10, "completion_tokens": 5}})]
u = db.scan_usage(usage_evs)
check("scan_usage 累计 input/output/cache",
      u == {"input_tokens": 310, "output_tokens": 115, "cache_read_tokens": 30}, str(u))

def t2b(root):
    CWD = r"C:\Users\demo\v11-no-cost"
    SID = "session-dddddddd-0000-0000-0000-000000000004"
    write_session(root, CWD, SID, 20, with_usage=False)  # 无 usage
    tasks = db.scan_tasks()
    check("无 usage 会话 → cost_est=None", tasks and tasks[0]["cost_est"] is None,
          str(tasks[0].get("cost_est") if tasks else None))
with_root(t2b)

print("== ③ CLI --status 输出包含标题/阶段 ==")
demo_tasks = [{"id": "aaaaaaaa", "cwd": r"C:\Users\demo\v11-status",
               "title": "状态查询演示任务", "status": "running",
               "stage": "步骤B", "stage_idx": 2, "stage_total": 3,
               "eta_at": "12:34:56", "cost_est": 0.012345},
              {"id": "bbbbbbbb", "cwd": r"C:\Users\demo\v11-old",
               "title": "已完成老任务", "status": "completed",
               "stage": "收尾", "stage_idx": 3, "stage_total": 3,
               "eta_at": None, "cost_est": None}]
st = db.status_text(demo_tasks)
check("输出包含状态 running/completed", "running" in st and "completed" in st, st)
check("输出包含标题", "状态查询演示任务" in st and "已完成老任务" in st, st)
check("输出包含阶段 k/N", "2/3" in st, st)
check("输出包含 ETA 与成本", "12:34:56" in st and "≈¥0.0123" in st, st)
check("无任务 → 暂无任务", db.status_text([]) == "暂无任务", repr(db.status_text([])))

old_argv = list(sys.argv)
try:
    sys.argv = ["dashboard.py", "--status"]
    a = db.build_parser().parse_args()
    check("--status 解析成功", a.status and not a.no_open, str(a))
    sys.argv = ["dashboard.py", "8124", "--no-open", "--status",
                "--feishu-webhook", "http://mock.feishu/hook"]
    a = db.build_parser().parse_args()
    check("--feishu-webhook 解析", a.feishu_webhook == "http://mock.feishu/hook", str(a))
    check("--status 与 port 可同用", a.status and a.port == 8124 and a.no_open, str(a))
    sys.argv = ["dashboard.py", "--feishu-webhook", "", "--no-feishu"]
    a = db.build_parser().parse_args()
    check("空 URL / --no-feishu 解析不崩",
          a.feishu_webhook == "" and a.no_feishu, str(a))
finally:
    sys.argv = old_argv

print("== ④ 飞书通知翻转检测（running→completed 触发一次且不重复）==")
def t4(root):
    CWD = r"C:\Users\demo\v11-flip"
    SID = "session-eeeeeeee-0000-0000-0000-000000000005"
    SID_DONE = "session-ffffffff-0000-0000-0000-000000000006"
    write_session(root, CWD, SID, 0)         # running（mtime=now）
    write_session(root, CWD, SID_DONE, 300)  # 启动时已 completed
    db._PARSE_CACHE.clear()

    sent = []
    real_post = db._feishu_post
    def mock_post(url, text):
        sent.append((url, text))
    db._feishu_post = mock_post
    old_hook, old_enabled = db._FEISHU_WEBHOOK, db._FEISHU_ENABLED
    old_prev, old_notified = dict(db._PREV_STATUS), set(db._NOTIFIED_IDS)
    db._FEISHU_WEBHOOK = "http://mock.feishu/hook"
    db._FEISHU_ENABLED = True
    try:
        tasks = db.scan_tasks()
        running = [t for t in tasks if t["id"] == "eeeeeeee"]
        done_at_start = [t for t in tasks if t["id"] == "ffffffff"]
        check("首扫 running 存在", running and running[0]["status"] == "running",
              str(running))
        check("启动时已 completed 任务存在",
              done_at_start and done_at_start[0]["status"] == "completed",
              str(done_at_start))
        db._check_flips(tasks)
        check("首扫（running）不通知", sent == [], str(sent))

        # 任务完成：把 mtime 改旧 → 翻转 running→completed
        path = os.path.join(root, sp.encode_cwd(CWD), SID, "session.jsonl.zstd")
        t_old = time.time() - 300
        os.utime(path, (t_old, t_old))
        db._PARSE_CACHE.clear()
        tasks2 = db.scan_tasks()
        flipped = [t for t in tasks2 if t["id"] == "eeeeeeee"]
        check("翻转后 status=completed", flipped and flipped[0]["status"] == "completed",
              str(flipped))
        db._check_flips(tasks2)
        check("running→completed 触发一次通知", len(sent) == 1, str(sent))
        if sent:
            url, text = sent[0]
            check("通知含标题/cwd/耗时/阶段",
                  "测试标题：成本显示与飞书通知" in text
                  and CWD in text and "耗时" in text and "阶段" in text, text)
            check("通知文本以 ✅ 开头", text.startswith("✅"), text)
            check("通知发往配置的 webhook URL", url == "http://mock.feishu/hook", url)

        db._check_flips(tasks2)
        check("同一会话只通知一次（不重复）", len(sent) == 1, str(sent))

        # 启动时已 completed 的任务从未通知
        db._check_flips([done_at_start[0]] if done_at_start else [])
        check("启动时已 completed 不通知", len(sent) == 1, str(sent))
    finally:
        db._feishu_post = real_post
        db._FEISHU_WEBHOOK, db._FEISHU_ENABLED = old_hook, old_enabled
        db._PREV_STATUS.clear(); db._PREV_STATUS.update(old_prev)
        db._NOTIFIED_IDS.clear(); db._NOTIFIED_IDS.update(old_notified)
with_root(t4)

print("== ④b 未启用飞书（--no-feishu / 无 webhook）→ 翻转不发送 ==")
def t4b(root):
    CWD = r"C:\Users\demo\v11-flip-disabled"
    SID = "session-11111111-0000-0000-0000-000000000007"
    write_session(root, CWD, SID, 0)
    db._PARSE_CACHE.clear()
    sent = []
    real_post = db._feishu_post
    def mock_post(url, text):
        sent.append((url, text))
    db._feishu_post = mock_post
    old_hook, old_enabled = db._FEISHU_WEBHOOK, db._FEISHU_ENABLED
    old_prev, old_notified = dict(db._PREV_STATUS), set(db._NOTIFIED_IDS)
    db._FEISHU_WEBHOOK = ""   # 空 webhook → 未启用
    db._FEISHU_ENABLED = False
    try:
        db._check_flips(db.scan_tasks())  # running
        path = os.path.join(root, sp.encode_cwd(CWD), SID, "session.jsonl.zstd")
        t_old = time.time() - 300
        os.utime(path, (t_old, t_old))
        db._PARSE_CACHE.clear()
        tasks = db.scan_tasks()           # completed
        db._check_flips(tasks)
        check("未启用飞书 → 翻转不发送", sent == [], str(sent))
    finally:
        db._feishu_post = real_post
        db._FEISHU_WEBHOOK, db._FEISHU_ENABLED = old_hook, old_enabled
        db._PREV_STATUS.clear(); db._PREV_STATUS.update(old_prev)
        db._NOTIFIED_IDS.clear(); db._NOTIFIED_IDS.update(old_notified)
with_root(t4b)

print("== ⑤ timeline 生成（事件 → 摘要列表条数/格式）==")
evs = [("step/start", {"step": 1})]
evs += [("reasoning-chunks", None)] * 5          # 连续 5 条 → 合并 ×5
evs += [("todo/write", {"todos": [
    {"content": "步骤A", "status": "completed"},
    {"content": "步骤B", "status": "in_progress"},
    {"content": "步骤C", "status": "pending"}]})]
evs += [("tool/call", {"name": "bash",
                       "arguments": '{"command": "pytest -q"}'})]
evs += [("assistant/message", {"message": {"content": [
    {"type": "text", "text": "正在分析需求"}]}})]
evs += [("session/title", {"title": "时间线标题"})]
evs += [("step/start", None)]                    # 无 time → t 为序号
base_t = 1787000000.0
times = [base_t + i * 10 for i in range(len(evs))]
times[-1] = None
tl = db.build_timeline(evs, times)
check("timeline 每条含 t/type/desc", all(
    isinstance(it, dict) and "t" in it and "type" in it and "desc" in it
    for it in tl), str(tl))
check("timeline 条数 = 7（chunk 合并为 1 条）", len(tl) == 7, str(len(tl)))
check("chunk 连续合并 desc=×5", any(
    it["type"] == "reasoning-chunks" and it["desc"] == "×5" for it in tl), str(tl))
check("tool/call desc 不含类型前缀（bash: pytest -q）", any(
    it["type"] == "tool/call" and it["desc"] == "bash: pytest -q" for it in tl), str(tl))
check("assistant/message desc 取文本", any(
    it["type"] == "assistant/message" and it["desc"] == "正在分析需求" for it in tl),
    str(tl))
check("todo desc 当前第 2 项/共 3 项", any(
    it["type"] == "todo/write" and it["desc"] == "当前第 2 项/共 3 项" for it in tl),
    str(tl))
check("无 time 条目 t 为序号", tl[-1]["type"] == "step/start"
      and tl[-1]["t"] == str(len(evs)), str(tl[-1]))
check("有 time 条目 t 为 HH:MM:SS", all(
    it["t"].count(":") == 2 and len(it["t"]) == 8 for it in tl[:-1]), str(tl))

# max_items 截断（取最近 N 条）
tl10 = db.build_timeline(evs, times, max_items=10)
check("max_items=10 ≥ 实际条数 → 全部保留", len(tl10) == 7, str(len(tl10)))
many = [("step/start", {"step": i}) for i in range(1, 61)]  # 步骤1..60
tl50 = db.build_timeline(many, [base_t + i for i in range(60)], max_items=50)
check("超 50 条 → 只保留最近 50 条", len(tl50) == 50, str(len(tl50)))
check("timeline 取最近（末条为步骤60）", tl50[-1]["desc"] == "步骤60",
      str(tl50[-1]))
check("max_items=0 → 空列表", db.build_timeline(evs, times, max_items=0) == [],
      str(db.build_timeline(evs, times, max_items=0)))

print("== ⑥ 任务对象字段齐全（含新字段 title/cost_est/timeline）==")
def t6(root):
    CWD = r"C:\Users\demo\v11-fields"
    SID = "session-22222222-0000-0000-0000-000000000008"
    write_session(root, CWD, SID, 20)
    tasks = db.scan_tasks()
    need = {"id", "cwd", "title", "status", "stage", "stage_idx", "stage_total",
            "stage_pct", "action", "eta_s", "eta_mode", "eta_at",
            "elapsed_s", "tail", "cost_est", "timeline"}
    check("任务字段齐全（16 字段）",
          tasks and need <= set(tasks[0].keys()), str(sorted(tasks[0].keys())))
    check("timeline 为摘要列表且非空",
          isinstance(tasks[0]["timeline"], list) and len(tasks[0]["timeline"]) > 0,
          str(tasks[0]["timeline"]))
    # _sid 为私有键（供飞书去重），不对外暴露 → live_payload 剔除
    with db._LOCK:
        db._STATE["tasks"] = tasks
    try:
        payload = db.live_payload()
        check("live_payload 不含 _sid 私有键",
              len(payload["tasks"]) == 1 and all("_sid" not in t
                                                 for t in payload["tasks"]),
              str(payload["tasks"]))
    finally:
        with db._LOCK:
            db._STATE["tasks"] = []
with_root(t6)

print(f"\n{'=' * 40}\n结果: {'全部通过 ✅' if fails == 0 else f'{fails} 项失败 ❌'}")
sys.exit(1 if fails else 0)
