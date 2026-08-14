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

解压必须用 zstandard 的 `stream_reader` 全量流式解压（`decompressobj` 只能解第一个 frame 是已知的坑）。看板每 4 秒增量轮询一次（跳过已处理行数），页面每 5 秒自动刷新。

## 安装与快速开始

```bash
pip install zstandard>=0.21

# 启动看板服务器（默认端口 8123，也可指定其他端口）
python dashboard.py 8123
```

浏览器打开 <http://127.0.0.1:8123>：

- **有任务运行时**：显示当前阶段（阶段 k/N + 名称 + 进度条）、ETA、最近动作与事件流；
- **无任务时**：显示"等待 dsh 任务开始..."，开始一个 dsh headless 任务后自动出现进度。

`/api/live` 返回当前任务状态 JSON（`running/tag/task/stage/stage_idx/stage_total/stage_pct/action/eta_s/eta_mode/eta_at/elapsed_s/tail/started_at/timeout_s`）。

## 阶段与 ETA 说明

- **阶段**来自模型的 `todo/write` 任务清单（取第一个未完成项，idx 从 1 开始）；模型**必须使用 todo 工具**维护清单才能显示阶段。建议在任务提示里引导，例如：

  ```
  【输出约定】请使用 todo 工具维护你的任务清单：每个主要步骤列一项
  （开始标记 in_progress、完成标记 completed，清单变化时更新）。
  ```

- **ETA** 为**阶段线性外推**：已走过阶段的平均耗时 × 剩余阶段数（无历史均值兜底）。数据不足（阶段数 < 2 或剩余为 0）时 `eta_mode` 为 `none`，不显示 ETA。

## 测试

```bash
python tests\make_fixtures.py          # 生成合成 fixtures（无真实会话数据）
python tests\test_session_progress.py  # 跑全部单测（无需 pytest），exit 0 全过
```

## 文件结构

```
dsh-progress-viz/
├── dashboard.py          # 独立看板服务器（全库扫描 + 4s 增量轮询 + ETA）
├── session_progress.py   # 会话事件流 → 阶段解析器（纯本地）
├── index.html            # 看板页面（深色主题，5s 自动刷新）
├── tests/
│   ├── make_fixtures.py              # 合成 fixtures 生成器
│   ├── test_session_progress.py      # 单测（无需 pytest）
│   └── fixtures/session-synthetic.jsonl.zstd
├── README.md
├── LICENSE
└── requirements.txt
```

## License

MIT（Copyright (c) 2026 dsh-progress-viz contributors）
