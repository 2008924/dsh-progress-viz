# dsh Progress Viz

实时可视化 dsh（headless 模式）任务执行过程的独立看板工具包——读取会话事件流，呈现阶段进度、ETA 与实时动态流，纯本地运行。

> **English:** dsh Progress Viz is a self-contained dashboard that visualizes dsh headless task execution in real time. It parses the local session event stream (`session.jsonl.zstd`) to show stage progress, ETA and a live activity feed — 100% local, no API calls, no tokens consumed.

![dashboard](docs/screenshot.png)

## 背景痛点

dsh 在 headless 模式下只输出**最终答案**，执行过程（读哪些文件、跑了什么命令、任务清单推进到哪一步）完全不可见。任务一跑就是几分钟甚至更久，期间无法判断：

- 任务是否还活着、卡在哪一步？
- 当前处于整个任务的哪个阶段、还剩多少阶段？
- 大概还要等多久？

## 原理

dsh 会把每次任务的完整事件流实时追加写入本地会话文件：

```
~/.dsh/sessions/<cwd编码>/<session-id>/session.jsonl.zstd
```

（zstd 压缩的 JSONL，每行一个事件，`type` 字段区分类型。）

本工具只关心三类事件：

| 事件类型 | 含义 | 用途 |
| --- | --- | --- |
| `todo/write` | 模型写入任务清单（`data.todos`，每项有 `content` + `status`） | **阶段数据源**：取第一个未完成项为当前阶段 |
| `step/start` | 步骤边界 | 无 todo 时的兜底计数 |
| `tool/call` | 工具调用（`data.name` + `data.arguments`） | 最近动作 + 事件流 |

解压必须用 zstandard 的 `stream_reader` 全量流式解压（`decompressobj` 只能解第一个 frame 是已知的坑）。看板每 4 秒轮询一次（重新扫描最近任务列表并逐任务解析），页面每 5 秒自动刷新。

## 安装与快速开始

```bash
pip install zstandard>=0.21

# 启动看板服务器（默认端口 8123，也可指定其他端口）
python dashboard.py 8123
```

浏览器打开 <http://127.0.0.1:8123>：

- **多任务分栏**：看板扫描 `~/.dsh/sessions` 下**全部**会话，只保留**最近 1 小时**内有写入
  （文件 mtime 距今 < 3600s）的任务，按 mtime 降序取**前 8 个**，以网格分栏展示
  （≥1400px 三列 / ≥900px 两列 / 其余单列，响应式）；
- **运行中卡片**（mtime 距今 ≤ 30s）：绿色状态点「正在运行」+ 任务 cwd + 会话 id（前 8 位）+
  已运行时长 + 阶段进度条（阶段 k/N + 名称）+ ETA（预计完成时刻 + 剩余时间 + 推算方式）+
  事件流（默认展开，max-height 滚动）；
- **已完成卡片**（mtime 距今 > 30s）：灰色「已完成」+ cwd + 耗时 + 最后阶段名
  （stage 或「步骤N」），事件流**默认折叠**（点击展开），避免信息过载；
- **无任务时**：显示"等待 dsh 任务开始..."，启动一个 dsh headless 任务后自动出现分栏；
- 标题区实时显示任务总数（如「共 3 个任务 · 每 5 秒自动刷新」）。

`/api/live` 返回最近任务列表 JSON：`{"tasks": [ {id, cwd, status, stage, stage_idx,
stage_total, stage_pct, action, eta_s, eta_mode, eta_at, elapsed_s, tail} ]}`，
无任务时 `tasks=[]`。每个任务的 ETA 独立计算（融合算法复用，历史会话排除任务自身）；
单个会话扫描/解析失败会静默跳过，不影响其他任务。

## 阶段与 ETA 说明

- **阶段**来自模型的 `todo/write` 任务清单（取第一个未完成项，idx 从 1 开始）；模型**必须使用 todo 工具**维护清单才能显示阶段。建议在任务提示里引导，例如：

  ```
  【输出约定】请使用 todo 工具维护你的任务清单：每个主要步骤列一项
  （开始标记 in_progress、完成标记 completed，清单变化时更新）。
  ```

- **ETA** 为**线性外推 + 历史均值融合**：
  - **线性分量 linear_s**：已走过阶段的平均耗时 × 剩余阶段数；
  - **历史分量 hist_s**：同 cwd 目录下**历史会话**（排除当前监控会话）耗时的**中位数**
    （每个历史会话耗时 = 事件流最大 `time` − 最小 `time`；无历史/扫描失败时视为不可用）；
  - **融合公式**：`eta = α·linear_s + (1−α)·hist_s`，α 随阶段进度自适应：
    `k≥3 → α=0.7`，`k==2 → α=0.5`，`k<2 → α=0`（纯历史均值）；
  - **回退链**（`eta_mode` 标记）：有阶段信息（k≥2 且 n>k）且历史可用 → `blend`；
    有阶段但无历史 → 纯 `linear`（α=1）；无阶段但有历史 → 纯 `history`（α=0）；都无 → `none`（不显示 ETA）；
  - 历史会话每次全量扫描（会话数少，代价可接受），结果按会话目录 mtime 缓存，
    4 秒轮询时 mtime 未变化直接复用 hist_s。

## 测试

```bash
python tests\make_fixtures.py          # 生成合成 fixtures（无真实会话数据）
python tests\test_session_progress.py  # 跑全部单测（无需 pytest），exit 0 全过
python tests\test_eta_blend.py         # ETA 融合算法单测（历史会话 fixture 生成到临时目录）
python tests\test_multi_pane.py        # 多任务分栏单测（1 小时窗口 / mtime 排序 / status 判定）
```

## 文件结构

```
dsh-progress-viz/
├── dashboard.py          # 独立看板服务器（全库扫描 + 4s 轮询 + 多任务分栏 + ETA）
├── session_progress.py   # 会话事件流 → 阶段解析器（纯本地）
├── index.html            # 看板页面（深色主题，多任务网格分栏，5s 自动刷新）
├── tests/
│   ├── make_fixtures.py              # 合成 fixtures 生成器（含历史会话 fixture）
│   ├── test_session_progress.py      # 单测（无需 pytest）
│   ├── test_eta_blend.py             # ETA 融合算法单测
│   ├── test_multi_pane.py            # 多任务分栏单测
│   └── fixtures/session-synthetic.jsonl.zstd
├── README.md
├── LICENSE
└── requirements.txt
```

## License

MIT（Copyright (c) 2026 dsh-progress-viz contributors）
