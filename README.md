# dsh Progress Viz

[![CI](https://github.com/2008924/dsh-progress-viz/actions/workflows/ci.yml/badge.svg)](https://github.com/2008924/dsh-progress-viz/actions/workflows/ci.yml) [![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) [![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/) [![DSH Market 收录徽章](https://raw.githubusercontent.com/2BingLing/dsh-market/master/assets/readme/badge-listed-zh.svg)](https://dsh.market/)

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

本工具关心的事件（v1.1 扩展后）：

| 事件类型 | 含义 | 用途 |
| --- | --- | --- |
| `todo/write` | 模型写入任务清单（`data.todos`，每项有 `content` + `status`） | **阶段数据源**：取第一个未完成项为当前阶段 |
| `step/start` | 步骤边界 | 无 todo 时的兜底计数 |
| `tool/call` | 工具调用（`data.name` + `data.arguments`） | 最近动作 + 事件流/时间线 |
| `session/title` | dsh 自动生成的任务标题（`data.title`） | **任务标题**（卡片优先显示，无标题回退 cwd basename） |
| `assistant/message` | 模型消息（`data.usage` 含 token 统计） | **成本估算**：累计 tokens × DeepSeek 定价常量 |

解压必须用 zstandard 的 `stream_reader` 全量流式解压（`decompressobj` 只能解第一个 frame 是已知的坑）。看板每 4 秒轮询一次（重新扫描最近任务列表并逐任务解析），页面每 5 秒自动刷新。

## 安装与快速开始

```bash
# 方式一：只装依赖，直接运行脚本（无需安装本项目）
pip install "zstandard>=0.21"
python dashboard.py 8123

# 方式二：按项目安装（pyproject.toml，自动带上 zstandard 依赖）
pip install .
python dashboard.py 8123        # 或 python -m dashboard 8123
```

> 本项目是**纯脚本型**包（无 console script 入口、不提供 `import` 模块），
> `pip install .` 只是安装依赖并注册元信息；运行方式与直接运行脚本完全一致：
> 在项目目录执行 `python dashboard.py [port]`（或 `python -m dashboard [port]`）。

启动行为：

- **自动打开浏览器**：服务器启动成功后自动用系统默认浏览器打开
  <http://127.0.0.1:<实际端口>>；
- **端口冲突自动递增**：指定端口（或默认 8123）被占用时自动 +1 重试
  （最多 5 次），并打印「端口 X 被占用，改用 Y」；连续 5 个端口都被占用才报错退出；
- **`--no-open`**：启动时不自动打开浏览器（如远程/无头环境）：
  `python dashboard.py 8123 --no-open`；
- **`--status`**：不启动服务器，直接打印当前任务状态表
  （状态 / 标题 / 阶段 k/N / ETA / 成本）后退出（exit 0），无任务打印「暂无任务」：
  `python dashboard.py --status`；
- **飞书完成通知**：`--feishu-webhook <URL>`（或环境变量 `FEISHU_WEBHOOK`）启用——
  任务从 running 变为 completed 时（看板轮询检测到状态翻转）向 webhook POST 一条
  文本消息「✅ dsh 任务完成：<标题>（<cwd>）· 耗时 <mm:ss> · 阶段 <最后阶段>」；
  同一会话只通知一次（缓存已通知会话 id），启动时已 completed 的任务不通知，
  发送失败静默（不阻塞看板）；`--no-feishu` 强制关闭。

浏览器打开 <http://127.0.0.1:8123>：

- **多任务分栏**：看板扫描 `~/.dsh/sessions` 下**全部**会话，只保留**最近 1 小时**内有写入
  （文件 mtime 距今 < 3600s）的任务，按 mtime 降序取**前 8 个**，以网格分栏展示
  （≥1400px 三列 / ≥900px 两列 / 其余单列，响应式）；
- **运行中置顶分区**（v1.2）：任务按状态分为「🟢 运行中」与「✅ 已完成」两个区块
  （运行中在上、已完成沉底），各带计数徽章；点击区块标题栏可整体折叠/展开（默认展开），
  长列表防信息过载；
- **运行中卡片**（mtime 距今 ≤ 30s）：绿色状态点「正在运行」+ 任务标题 + cwd + 会话 id（前 8 位）+
  已运行时长 + 阶段进度条（阶段 k/N + 名称）+ ETA（预计完成时刻 + 剩余时间 + 推算方式）+
  成本估算（有 usage 数据才显示）+ 「详情」展开区（点击展开完整时间线，等宽字体）+
  事件流（默认展开，max-height 滚动）；
- **已完成卡片**（mtime 距今 > 30s）：灰色「已完成」+ 标题 + cwd + 耗时 + 最后阶段名
  （stage 或「步骤N」），事件流**默认折叠**（点击展开），避免信息过载；
- **无任务时**：显示「等待 dsh 任务开始...」，启动一个 dsh headless 任务后自动出现分栏；
- 标题区实时显示任务总数与运行中数（如「共 8 个任务 · 运行中 2 · 每 5 秒自动刷新」）。

`/api/live` 返回最近任务列表 JSON：`{"tasks": [ {id, cwd, title, status, stage,
stage_idx, stage_total, stage_pct, action, eta_s, eta_mode, eta_at, elapsed_s,
tail, cost_est, timeline} ]}`，无任务时 `tasks=[]`。每个任务的 ETA 独立计算
（融合算法复用，历史会话排除任务自身）；单个会话扫描/解析失败会静默跳过，
不影响其他任务。

## v1.1 新功能说明

- **成本显示（A）**：解析 `assistant/message`（或 `tool/result`）事件的
  `data.usage`（实测本机格式：`inputTokens` / `outputTokens` / `cacheReadTokens`），
  按模块级常量 `PRICES`（DeepSeek 官方定价，元/百万 tokens，注释标明价格与日期、
  可改）估算成本，卡片显示「≈¥0.0123」并标注**估算**。事件流无 usage 数据时
  `cost_est` 置 `None`，前端不显示（不硬编假数据）。
- **任务标题（D）**：解析 `session/title` 事件的 `data.title`（取最后一个非空），
  任务卡片优先显示标题；无标题回退 cwd 的 basename；再回退完整 cwd。
- **CLI 状态查询（C）**：`--status` 不启动服务器，直接打印当前任务状态表。
- **飞书完成通知（B）**：`--feishu-webhook` / `FEISHU_WEBHOOK` 启用；任务
  running→completed 翻转时通知一次（去重），启动时已 completed 不通知，失败静默。
- **详情时间线（E）**：`/api/live` 任务对象新增 `timeline` 字段（`[{t, type, desc}]`，
  最多 50 条，chunk 连续合并、取最近）；运行中卡片「详情」点击展开。

## v1.2 看板 UI 更新

- **运行中置顶**：任务按状态分为「运行中」「已完成」两个区块，运行中在上、已完成沉底；
- **区块折叠**：两个区块均可点击标题栏折叠/展开（默认展开），长列表防信息过载；
- **计数徽章**：标题与区块同步显示「共 N 个任务 · 运行中 M」实时计数；
- **跨平台修复**：cwd 标题回退在 Linux/macOS 上正确取 basename（反斜杠归一化为正斜杠，
  修复 test_v11 在 macos/ubuntu 上的失败）。

## 插件版（dsh-progress-viz-plugin）

除了「独立版」（看板直接 zstd 解析会话文件），本工具提供 **cordis 插件版**：
插件挂载到 dsh profile（任务执行的地方，如 headless），**实时监听会话事件**，
只保留语义事件（`todo/write`、`step/start`、`step/end`、`tool/call`、
`tool/result`、`assistant/message`、`turn/start`、`turn/end`、`session/title`、
`session`），过滤 chunk 等中间态噪音（`assistant/chunk`、`reasoning-chunks`、
`tool-call-chunks`、`text-chunks`、`request/*` 等），原子重写
`<DSH_HOME>/progress/<session-id>.json`（及 `current.json`）供看板消费。

**与独立版的关系**：看板新增数据源，**优先读插件输出**（实时、已过滤噪音）；
`<DSH_HOME>/progress/` 缺失或没有文件时，**自动回退**现有 zstd 解析路径
（行为不变，新增优先级不影响回退）。`/api/live` 字段语义不变（插件任务
`eta_*` 为 `None`、`tail` 由 `timeline` 派生）。

### 安装（dsh plugin add）

插件源码在 `plugin/` 目录（TypeScript、ESM、`lib/index.js` 产物）。构建并挂载：

```bash
cd publish/dsh-progress-viz/plugin
pnpm install --registry https://registry.npmmirror.com   # 国内 registry
pnpm build                                                # tsc → lib/index.js
# 回到任意目录，把插件挂到 headless profile（本地目录绝对路径）：
dsh plugin --profile headless add <plugin 目录绝对路径>
dsh --profile headless --dump-config   # 验证输出包含 progress-viz
```

> `dsh plugin add` 会执行 `pnpm add <路径>` 并把本包加入 profile 的
> `dsh.profile.bundles`（插件包声明了 `dsh.bundle.patch`）。若环境不支持
> `dsh plugin`，可手动把插件加入 profile `package.json` 的 `dependencies`
> 与 `dsh.profile.bundles` 数组后执行 `pnpm install`。插件零配置可挂载。

### 输出格式

每个会话一个文件：`<DSH_HOME>/progress/<session-id>.json`（原子写：临时文件 +
rename，每次语义事件更新重写）；会话结束（`session/disposed` 或空闲超时）后
标记 `finished: true` 并保留文件；新会话开始（`session/created`）时重置状态。

```json
{
  "session_id": "session-xxxxxxxx-...",
  "title": "任务标题", "cwd": "C:\\work",
  "stage": "当前阶段", "stage_idx": 2, "stage_total": 3, "stage_pct": 67,
  "action": "运行 bash 命令: pytest -q",
  "cost_est": 0.0123, "elapsed_s": 42,
  "updated_at": "2026-08-15T12:00:00.000Z", "finished": false,
  "timeline": [{"t": "12:00:01", "type": "todo/write", "desc": "当前第 2 项/共 3 项"}]
}
```

阶段逻辑与独立版一致（todo 第一个未完成项优先、`step/start` 计数兜底）；
成本按同一 DeepSeek 定价常量估算（无 usage → `null`）。详见
[plugin/README.md](plugin/README.md)。

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
python tests/make_fixtures.py          # 生成合成 fixtures（无真实会话数据）
python tests/test_session_progress.py  # 会话解析单测（无需 pytest），exit 0 全过
python tests/test_eta_blend.py         # ETA 融合算法单测（历史会话 fixture 生成到临时目录）
python tests/test_multi_pane.py        # 多任务分栏单测（1 小时窗口 / mtime 排序 / status 判定）
python tests/test_tail_format.py       # tail 可读性单测（chunk 合并 / 时间戳 / 动作高亮 / 截断）
python tests/test_port.py              # 端口冲突自动递增单测（socket 占用模拟，随机高位端口）
python tests/test_cache.py             # 解析缓存单测（mtime 未变不重新解压 / 修改后重解析 / 缓存清理）
python tests/test_v11.py               # v1.1 五项增强单测（标题 / 成本 / CLI --status / 飞书通知 / 时间线）
python tests/test_plugin_progress.py   # 插件版数据源单测（优先级 / 回退 / status / 去重 / 窗口）
```

GitHub Actions CI（`.github/workflows/ci.yml`）在 push 到 main 与 pull_request 时，
以 ubuntu / macos / windows × Python 3.9 / 3.11 六种组合逐套运行上述单测。

## 常见问题

遇到「zstandard 未安装」「看不到任务」「端口被占用」「路径显示异常」「显示步骤N
而不是任务名」「任务完成后卡片还在」「如何卸载/停止看板」等问题，请查阅
**[docs/FAQ.md](docs/FAQ.md)**（7 问 7 答）。

## 文件结构

```
dsh-progress-viz/
├── .github/workflows/ci.yml # GitHub Actions CI（3 平台 × 2 Python 矩阵）
├── dashboard.py          # 独立看板服务器（全库扫描 + 4s 轮询 + 解析缓存 + 多任务分栏 +
│                         #   ETA + 成本估算 + 任务标题 + 详情时间线 + 飞书完成通知 + CLI --status）
├── session_progress.py   # 会话事件流 → 阶段解析器（纯本地）
├── index.html            # 看板页面（深色主题，多任务网格分栏，标题/成本/详情时间线，5s 自动刷新）
├── pyproject.toml        # 项目元信息 / 依赖声明（pip install . 用）
├── tests/
│   ├── make_fixtures.py              # 合成 fixtures 生成器（含历史会话 fixture）
│   ├── test_session_progress.py      # 单测（无需 pytest）
│   ├── test_eta_blend.py             # ETA 融合算法单测
│   ├── test_multi_pane.py            # 多任务分栏单测
│   ├── test_tail_format.py           # tail 可读性单测
│   ├── test_port.py                  # 端口冲突自动递增单测
│   ├── test_cache.py                 # 解析缓存单测
│   ├── test_v11.py                   # v1.1 五项增强单测（标题/成本/CLI/飞书/时间线）
│   ├── test_plugin_progress.py       # 插件版数据源单测（优先级/回退/status/去重/窗口）
│   └── fixtures/session-synthetic.jsonl.zstd
├── docs/
│   ├── FAQ.md            # 常见问题（7 问 7 答）
│   └── screenshot.png    # 看板效果图
├── README.md
├── LICENSE
└── requirements.txt
```

## License

MIT（Copyright (c) 2026 dsh-progress-viz contributors）
