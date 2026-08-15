/**
 * dsh-progress-viz 插件 —— 类型定义（仅类型，无运行时代码）。
 *
 * 进度 JSON 结构与看板 publish/dsh-progress-viz/dashboard.py 的
 * /api/live 任务字段对齐，方便看板直接消费插件输出。
 */

/** 时间线条目：与看板 /api/live 的 timeline 字段对齐。 */
export interface TimelineItem {
  /** HH:MM:SS（事件 time 毫秒时间戳 → 本地时间） */
  t: string
  /** 事件类型（如 tool/call、step/start） */
  type: string
  /** 中文摘要（format_action 同款规则，chunk 噪音已过滤） */
  desc: string
}

/**
 * 进度 JSON 顶层结构（每次语义事件更新时原子重写：
 * 先写 <file>.tmp 再 rename，避免看板读到半截文件）。
 */
export interface ProgressJson {
  /** 完整会话 id（形如 session-xxxxxxxx-...） */
  session_id: string
  /** 任务标题（session/title 事件的 data.title；无则 null） */
  title: string | null
  /** 会话工作目录（cwd；无则 null） */
  cwd: string | null
  /** 当前阶段名：todo 第一个未完成项；无 todo 时「步骤N」；均无则 null */
  stage: string | null
  /** 当前阶段序号（1 起；未知为 0） */
  stage_idx: number
  /** 阶段总数（无 todo 时为 0，表示未知） */
  stage_total: number
  /** 阶段进度百分比 0-100（stage_total 为 0 时为 0） */
  stage_pct: number
  /** 最近动作中文摘要（最近 tool/call；无则 null） */
  action: string | null
  /** 成本估算（元；DeepSeek 定价常量估算，无 usage 数据则 null） */
  cost_est: number | null
  /** 已耗时（秒，自会话创建起） */
  elapsed_s: number
  /** 更新时间（ISO 8601） */
  updated_at: string
  /** 会话是否已结束（finished: true 后文件保留、不再更新） */
  finished: boolean
  /** 语义事件时间线（≤ timelineMax 条，取最近；chunk 噪音已过滤） */
  timeline: TimelineItem[]
}

/** 插件配置（全部可选，带默认值，保证零配置即可挂载）。 */
export interface ProgressVizConfig {
  /** 输出目录；缺省 <DSH_HOME>/progress（DSH_HOME 未设置时 ~/.dsh/progress） */
  outDir?: string
  /** turn/end 后多少毫秒无新语义事件即标记 finished（默认 15000） */
  idleTimeoutMs?: number
  /** 时间线最大条数（默认 50，超限取最近 N 条） */
  timelineMax?: number
  /** 是否同时写 current.json（指向最新会话，默认 true） */
  writeCurrent?: boolean
}

/** 内部会话进度状态（内存态，随语义事件推进）。 */
export interface SessionProgressState {
  /** 完整会话 id */
  sessionId: string
  /** 任务标题（session/title 事件更新；无则 null） */
  title: string | null
  /** 会话工作目录 */
  cwd: string | null
  /** 会话创建时间（epoch 毫秒） */
  startedAt: number
  /** 最近一次语义事件时间（epoch 毫秒；空闲判定用） */
  lastEventAt: number
  /** 最近一次 todo/write 的完整清单（原始项；null = 尚无 todo） */
  todos: unknown[] | null
  /** todo/write 写入次数（无 status 字段清单的兜底定位规则用） */
  todoCount: number
  /** 是否已见过 usage 数据（未见 → cost_est 置 null；全 0 → 0.0） */
  usageSeen: boolean
  /** step/start 计数（无 todo 时的兜底阶段） */
  stepCount: number
  /** 最近动作中文摘要（最近 tool/call） */
  action: string | null
  /** 时间线条目（≤ timelineMax 条，取最近） */
  timeline: TimelineItem[]
  /** 累计输入 tokens（assistant/message 的 usage.inputTokens） */
  inputTokens: number
  /** 累计输出 tokens（usage.outputTokens） */
  outputTokens: number
  /** 累计缓存命中 tokens（usage.cacheReadTokens） */
  cacheReadTokens: number
  /** 是否已标记结束（finished: true 后不再更新） */
  finished: boolean
  /** 空闲标记定时器（turn/end 后启动，超时无新事件 → finished） */
  idleTimer: ReturnType<typeof setTimeout> | undefined
  /** 本会话进度文件路径（<outDir>/<session-id>.json） */
  filePath: string
}
