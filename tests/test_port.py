# -*- coding: utf-8 -*-
"""dsh-progress-viz 端口冲突自动递增单测（纯本地，不烧 token；随机高位端口，不占常用端口）

运行方式：python tests\test_port.py（无需 pytest），exit 0 全过。

覆盖（按 spec）：
  ① 端口被占用 → 自动 +1 递增，选到下一个空闲端口（并打印「端口 X 被占用，改用 Y」）
  ② 连续 5 个端口全被占用 → 报错退出（SystemExit）
  ③ 起始端口本就空闲 → 原样返回（不递增、无提示）
  ④ argparse 向后兼容：python dashboard.py 8123 / --no-open / 默认 8123
"""
import contextlib
import io
import os
import random
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)  # publish\dsh-progress-viz
sys.path.insert(0, PKG)      # 使 dashboard.py 可导入
import dashboard as db

fails = 0


def check(name, cond, detail=""):
    global fails
    if cond:
        print(f"  PASS {name}")
    else:
        fails += 1
        print(f"  FAIL {name} {detail}")


def bind_port(port):
    """绑定 127.0.0.1:port 并保持占用（返回 socket，调用方负责 close）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    return s


def free_range(n):
    """找一个随机高位起始端口 P，且 P..P+n-1 全部空闲（避免踩到真实服务）。"""
    while True:
        p = random.randint(20000, 50000)
        socks = []
        try:
            for i in range(n):
                socks.append(bind_port(p + i))
        except OSError:
            for s in socks:
                s.close()
            continue
        for s in socks:
            s.close()
        return p


print("== 端口被占用 → 自动 +1 递增 ==")
def t1():
    p = free_range(2)   # P 与 P+1 当前都空闲
    s = bind_port(p)    # 占住 P
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got = db.pick_free_port(p)
        check("占住 P 后选到下一个空闲端口 P+1", got == p + 1, f"got={got}")
        msg = buf.getvalue()
        check("打印「端口 X 被占用，改用 Y」",
              f"端口 {p} 被占用，改用 {p + 1}" in msg, repr(msg))
    finally:
        s.close()
t1()

print("== 连续 5 个端口全占用 → 报错退出 ==")
def t2():
    p = free_range(5)   # P..P+4 当前都空闲
    socks = [bind_port(p + i) for i in range(5)]
    try:
        raised = None
        try:
            db.pick_free_port(p, tries=5)
        except SystemExit as e:
            raised = e
        check("5 个端口全占用 → SystemExit 报错退出",
              raised is not None, f"raised={raised}")
        if raised is not None:
            check("报错信息含被占用端口范围", str(p) in str(raised),
                  repr(str(raised)))
    finally:
        for s in socks:
            s.close()
t2()

print("== 起始端口空闲 → 原样返回（不递增、无提示）==")
def t3():
    p = free_range(1)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = db.pick_free_port(p)
    check("空闲端口原样返回", got == p, f"got={got}")
    check("无「被占用」提示", "被占用" not in buf.getvalue(), repr(buf.getvalue()))
t3()

print("== argparse 向后兼容 ==")
def t4():
    old = list(sys.argv)
    try:
        sys.argv = ["dashboard.py", "8123"]
        a = db.build_parser().parse_args()
        check("python dashboard.py 8123 → port=8123 且不 no_open",
              a.port == 8123 and not a.no_open, str(a))
        sys.argv = ["dashboard.py", "8124", "--no-open"]
        a = db.build_parser().parse_args()
        check("--no-open → no_open=True", a.port == 8124 and a.no_open, str(a))
        sys.argv = ["dashboard.py"]
        a = db.build_parser().parse_args()
        check("无参数 → 默认 8123", a.port == 8123 and not a.no_open, str(a))
    finally:
        sys.argv = old
t4()

print(f"\n{'=' * 40}\n结果: {'全部通过 ✅' if fails == 0 else f'{fails} 项失败 ❌'}")
sys.exit(1 if fails else 0)
