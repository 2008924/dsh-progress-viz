# dsh-progress-viz-plugin

dsh 进度可视化插件：实时监听会话事件（`ctx.on('session/event')`），只保留
「语义事件」推进进度状态，并原子重写 `<DSH_HOME>/progress/<session-id>.json`
（及 `current.json`），供 [dsh-progress-viz 看板](../dashboard.py) 实时消费。
与「独立版」（看板直接 zstd 解析会话文件）的关系：插件版输出**已过滤噪音**的
实时进度 JSON，看板优先读取插件输出，缺失时回退独立版 zstd 解析。

## 安装

插件作为 cordis 插件挂载到目标 profile（任务执行的地方，如 headless）：

```bash
# 在 dsh-project 根目录执行（registry 用国内 npmmirror）
pnpm install --registry https://registry.npmmirror.com
pnpm build
dsh plugin --profile headless add <本插件目录绝对路径>
# 验证：dsh --profile headless --dump-config 输出应包含 progress-viz
```

> 说明：`dsh plugin add` 会执行 `pnpm add <路径>` 并把本包加入
> `dsh.profile.bundles`（本包声明了 `dsh.bundle.patch`）。若环境不支持
> `dsh plugin`，可手动把插件加入 profile 的 `package.json` 的
> `dependencies` 与 `dsh.profile.bundles` 数组后执行 `pnpm install`。

## 配置

全部可选（零配置即可挂载，见 `cordis.patch.yml`）：

| 键 | 默认 | 说明 |
|---|---|---|
| `outDir` | `<DSH_HOME>/progress` | 进度 JSON 输出目录 |
| `idleTimeoutMs` | `15000` | turn/end 后多久无新语义事件即标记 finished |
| `timelineMax` | `50` | 时间线条目上限（取最近 N 条） |
| `writeCurrent` | `true` | 是否同时写 `current.json`（指向最新会话） |

## 输出格式

每个会话一个文件：`<outDir>/<session-id>.json`（原子写：临时文件 + rename），
每次语义事件更新重写；会话结束（`session/disposed` 或空闲超时）后标记
`finished: true` 并保留文件；新会话开始（`session/created`）时重置状态。

```json
{
  "session_id": "session-xxxxxxxx-xxxx-...",
  "title": "任务标题",
  "cwd": "C:\\work",
  "stage": "当前阶段（todo 第一个未完成项 / 步骤N）",
  "stage_idx": 2,
  "stage_total": 3,
  "stage_pct": 67,
  "action": "运行 bash 命令: pytest -q",
  "cost_est": 0.0123,
  "elapsed_s": 42,
  "updated_at": "2026-08-15T12:00:00.000Z",
  "finished": false,
  "timeline": [
    { "t": "12:00:01", "type": "todo/write", "desc": "当前第 2 项/共 3 项" },
    { "t": "12:00:05", "type": "tool/call", "desc": "运行 bash 命令: pytest -q" }
  ]
}
```

字段语义与看板 `/api/live` 任务字段对齐；`timeline` 最多 `timelineMax` 条
（默认 50），chunk 类中间态事件（assistant/chunk、reasoning-chunks、
tool-call-chunks、text-chunks、request/* 等）已被过滤，不进入时间线。

## 语义事件与过滤

保留（写入进度状态）：`todo/write`、`step/start`、`step/end`、`tool/call`、
`tool/result`、`assistant/message`、`turn/start`、`turn/end`、
`session/title`、`session`。
过滤（噪音，不写入）：`assistant/chunk`、`reasoning-chunks`、
`tool-call-chunks`、`text-chunks`、`agent/inbox/spliced`、`request/*` 等。

## 构建

```bash
pnpm install --registry https://registry.npmmirror.com
pnpm build   # tsc → lib/index.js
```

## 开发说明

- `src/index.ts`：插件主体（事件监听 + 状态机 + 原子写）。
- `src/types.ts`：类型（进度 JSON / 配置 / 内部状态）。
- 阶段逻辑与 `../session_progress.py` 的 `build_progress` 保持一致；
  成本估算价格常量与 `../dashboard.py` 的 `PRICES` 一致（DeepSeek 官方定价
  元/百万 tokens，估算值，字段名 `cost_est` 标注）。
