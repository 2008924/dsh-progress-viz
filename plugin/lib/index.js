/**
 * dsh-progress-viz 插件 —— 实时监听会话事件，输出进度 JSON 供看板消费。
 *
 * 数据流：ctx.on('session/event') 监听所有会话事件，只保留「语义事件」
 * （todo/write、step/start、step/end、tool/call、tool/result、
 * assistant/message、turn/start、turn/end、session/title、session），
 * 过滤 chunk 等中间态噪音（assistant/chunk、reasoning-chunks、
 * tool-call-chunks、text-chunks、agent/inbox/spliced、request/* 等）；
 * 每个语义事件推进内存状态并原子重写
 * <DSH_HOME>/progress/<session-id>.json（及 current.json，指向最新会话）。
 *
 * 阶段逻辑与 publish/dsh-progress-viz/session_progress.py 的 build_progress
 * 保持一致：todo 清单优先（第一个未完成项），无 todo 时 step/start 计数
 * （「步骤N」）；动作摘要复用 format_action 同款中文规则。
 * 成本按 DeepSeek 官方定价常量估算（assistant/message 的 usage 累计 tokens）。
 * 会话结束（session/disposed，或 turn/end 后 idleTimeoutMs 无新事件）时
 * 标记 finished: true 并保留文件；新会话开始（session/created）时重置状态。
 *
 * @module dsh-progress-viz-plugin
 */
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { mkdirSync, renameSync, writeFileSync } from 'node:fs';
import Schema from '@deepseek-ai/schemastery';
/** 稳定插件名（loader patch 的 row id 与此一致）。 */
export const name = 'progress-viz';
/**
 * 本插件不注入业务服务：只通过 ctx.on 订阅会话生命周期与事件流
 * （session/created、session/event、session/disposed）。
 */
export const inject = [];
// DeepSeek 官方定价常量（元/百万 tokens，与 dashboard.py 的 PRICES 一致）：
//   输入（缓存未命中）¥2、输出 ¥8、缓存命中 ¥0.5 —— deepseek-chat 价近似。
//   本机会话模型为 deepseek-v4-flash，按 deepseek-chat 价估算（字段 cost_est 标注）。
const PRICES = { input: 2.0, output: 8.0, cache_read: 0.5 };
/**
 * 语义事件白名单：只把这些事件写入进度状态；其余（chunk 等中间态）一律
 * 过滤，避免看板时间线被刷屏。session 事件（会话 header）防御性保留。
 */
const SEMANTIC_TYPES = new Set([
    'todo/write', 'step/start', 'step/end', 'tool/call', 'tool/result',
    'assistant/message', 'turn/start', 'turn/end', 'session/title', 'session',
]);
/** 插件配置（全部可选带默认值：零配置即可挂载）。 */
export const Config = Schema.object({
    outDir: Schema.string().default(''),
    idleTimeoutMs: Schema.number().default(15000),
    timelineMax: Schema.number().default(50),
    writeCurrent: Schema.boolean().default(true),
});
/** 输出目录缺省值：<DSH_HOME>/progress（DSH_HOME 未设置时 ~/.dsh/progress）。 */
function defaultOutDir() {
    const home = process.env.DSH_HOME?.trim() || join(homedir(), '.dsh');
    return join(home, 'progress');
}
/** epoch 毫秒 → HH:MM:SS（本地时间），与看板时间线条目一致。 */
function formatTime(ms) {
    const d = new Date(ms);
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
/** 路径 basename（兼容 Windows 反斜杠，避免依赖平台语义）。 */
function baseName(p) {
    return p.replaceAll('\\', '/').split('/').pop() ?? p;
}
/**
 * 工具调用 → 简短中文动作描述（format_action 同款规则）：
 * bash 取命令前 60 字符；read/write 取文件名；其他取工具名。
 */
function formatAction(data) {
    const toolName = String(data.name ?? '');
    if (!toolName)
        return null;
    let args = data.arguments;
    if (typeof args === 'string') {
        try {
            const parsed = JSON.parse(args);
            if (parsed !== null && typeof parsed === 'object')
                args = parsed;
        }
        catch {
            // arguments 不是 JSON → 按原样处理
        }
    }
    if (toolName === 'bash') {
        let cmd = '';
        if (args !== null && typeof args === 'object') {
            const rec = args;
            cmd = String(rec.command ?? rec.cmd ?? '');
        }
        else if (typeof args === 'string') {
            cmd = args;
        }
        return `运行 bash 命令: ${cmd.slice(0, 60)}`;
    }
    if (toolName === 'read' || toolName === 'write') {
        const fp = args !== null && typeof args === 'object'
            ? args.file_path
            : undefined;
        if (fp) {
            const verb = toolName === 'read' ? '读取文件' : '写入文件';
            return `${verb}: ${baseName(String(fp))}`;
        }
        return toolName;
    }
    return toolName;
}
/** assistant/message 的 data → 关键文本（text 段优先，回退 reasoning）。 */
function messageText(data) {
    const msg = data.message;
    if (msg === null || typeof msg !== 'object')
        return '';
    const content = msg.content;
    const parts = [];
    if (Array.isArray(content)) {
        for (const c of content) {
            if (c !== null && typeof c === 'object'
                && c.type === 'text'
                && c.text) {
                parts.push(String(c.text));
            }
        }
        if (parts.length === 0) {
            for (const c of content) {
                if (c !== null && typeof c === 'object'
                    && c.type === 'reasoning'
                    && c.text) {
                    parts.push(String(c.text));
                }
            }
        }
    }
    else if (typeof content === 'string') {
        parts.push(content);
    }
    return parts.join(' ').trim();
}
/** 从事件 data 提取 todo 清单（兼容 todos/items 两种字段名）。 */
function todoItems(data) {
    if (!data)
        return [];
    const raw = data.todos ?? data.items;
    return Array.isArray(raw) ? raw : [];
}
/**
 * 从任务清单选当前阶段项 → [1-based idx, total]（与 session_progress 一致）：
 * 有 status 字段 → 第一个未完成项（completed 跳过）；全部完成取最后一项；
 * 无 status 字段 → 清单第 (已写次数-1) 项（越界钳制到 [1, total]）。
 */
function pickTodoStage(items, todoCount) {
    const total = items.length;
    const hasStatus = items.some(it => it !== null && typeof it === 'object'
        && 'status' in it);
    if (hasStatus) {
        for (let i = 0; i < total; i++) {
            const it = items[i];
            const status = it !== null && typeof it === 'object'
                ? it.status : undefined;
            if (status !== 'completed')
                return [i + 1, total];
        }
        return [total, total];
    }
    const pos = Math.max(1, Math.min(todoCount - 1, total));
    return [pos, total];
}
/** 清单项 → 标题文本（兼容 content/title 两种字段名）。 */
function itemText(it) {
    if (it !== null && typeof it === 'object') {
        const rec = it;
        return String(rec.content ?? rec.title ?? '');
    }
    return String(it);
}
/** 从状态计算当前阶段（todo 优先，step/start 兜底）。 */
function computeStage(state) {
    if (state.todos !== null && state.todos.length > 0) {
        const [idx, total] = pickTodoStage(state.todos, state.todoCount);
        return { stage: itemText(state.todos[idx - 1]) || null, idx, total };
    }
    if (state.stepCount > 0) {
        return { stage: `步骤${state.stepCount}`, idx: state.stepCount, total: 0 };
    }
    return { stage: null, idx: 0, total: 0 };
}
/** 累计 assistant/message 的 usage tokens（camelCase 主，snake_case 兼容）。 */
function accumulateUsage(state, data) {
    const u = data.usage;
    if (u === null || typeof u !== 'object')
        return;
    const rec = u;
    const num = (key) => {
        const v = rec[key];
        return typeof v === 'number' && Number.isFinite(v) ? v : 0;
    };
    state.inputTokens += num('inputTokens') + num('prompt_tokens');
    state.outputTokens += num('outputTokens') + num('completion_tokens');
    state.cacheReadTokens += num('cacheReadTokens');
    state.usageSeen = true;
}
/** 按 DeepSeek 定价常量估算成本（元）；未见 usage → null（禁止硬编假数据）。 */
function estimateCost(state) {
    if (!state.usageSeen)
        return null;
    const yuan = state.inputTokens * PRICES.input
        + state.outputTokens * PRICES.output
        + state.cacheReadTokens * PRICES.cache_read;
    return yuan / 1_000_000.0;
}
/** 单条时间线描述（中文摘要，与看板 _timeline_desc 同规则）。 */
function timelineDesc(type, data, state) {
    switch (type) {
        case 'tool/call':
            return formatAction(data ?? {}) ?? '';
        case 'assistant/message':
            return messageText(data ?? {}).slice(0, 80);
        case 'todo/write': {
            const items = todoItems(data);
            if (items.length > 0) {
                const [idx, total] = pickTodoStage(items, state.todoCount);
                return `当前第 ${idx} 项/共 ${total} 项`;
            }
            return '';
        }
        case 'step/start':
            return `步骤${String((data ?? {}).step ?? '')}`;
        case 'step/end':
            return `步骤${String((data ?? {}).step ?? '')} 完成`;
        case 'turn/start':
            return '回合开始';
        case 'turn/end':
            return '回合结束';
        case 'session/title':
            return String((data ?? {}).title ?? '');
        case 'session':
            return '会话开始';
        case 'tool/result': {
            const err = data !== undefined && typeof data.error === 'object'
                ? data.error.name : undefined;
            return err ? `工具失败: ${String(err)}` : '工具执行完成';
        }
        default:
            return '';
    }
}
/** 原子写：先写 <file>.tmp 再 rename（看板轮询永远读不到半截文件）。 */
function writeJsonAtomic(filePath, json, logger) {
    try {
        mkdirSync(dirname(filePath), { recursive: true });
        const tmp = `${filePath}.tmp`;
        writeFileSync(tmp, `${JSON.stringify(json, null, 2)}\n`, 'utf-8');
        renameSync(tmp, filePath);
    }
    catch (error) {
        logger.warn(`progress-viz: 写入进度文件失败 ${filePath}: ${String(error)}`);
    }
}
/** 会话 id 安全化（文件名用；默认 session-xxx 已安全，防御性替换）。 */
function safeId(id) {
    return id.replace(/[^A-Za-z0-9._-]/g, '_');
}
/** 新建会话状态（文件路径 = <outDir>/<session-id>.json）。 */
function createState(session, outDir) {
    const sessionId = String(session.header.id);
    return {
        sessionId,
        title: null,
        cwd: session.header.cwd ?? null,
        startedAt: session.header.createdAt,
        lastEventAt: session.header.createdAt,
        todos: null,
        todoCount: 0,
        stepCount: 0,
        action: null,
        timeline: [],
        inputTokens: 0,
        outputTokens: 0,
        cacheReadTokens: 0,
        usageSeen: false,
        finished: false,
        idleTimer: undefined,
        filePath: join(outDir, `${safeId(sessionId)}.json`),
    };
}
/** 状态 → 进度 JSON（字段与看板 /api/live 任务字段对齐）。 */
function buildProgressJson(state, timelineMax) {
    const { stage, idx, total } = computeStage(state);
    const now = Date.now();
    return {
        session_id: state.sessionId,
        title: state.title,
        cwd: state.cwd,
        stage,
        stage_idx: idx,
        stage_total: total,
        stage_pct: total > 0 ? Math.round((idx / total) * 100) : 0,
        action: state.action,
        cost_est: estimateCost(state),
        elapsed_s: Math.max(0, Math.floor((now - state.startedAt) / 1000)),
        updated_at: new Date(now).toISOString(),
        finished: state.finished,
        timeline: state.timeline.slice(-timelineMax),
    };
}
/** 处理一个语义事件：推进状态 + 追加时间线 + 触发写入。 */
function handleEvent(state, event, cfg, write) {
    if (state.finished)
        return; // 已结束的会话不再更新（文件保留）
    const type = event.type;
    if (!SEMANTIC_TYPES.has(type))
        return; // 过滤 chunk 等中间态噪音
    const data = event.data;
    // 任何语义事件都说明会话仍活跃：取消之前的空闲定时器（turn/end 会重新启动）
    if (state.idleTimer !== undefined) {
        clearTimeout(state.idleTimer);
        state.idleTimer = undefined;
    }
    state.lastEventAt = event.time;
    switch (type) {
        case 'todo/write': {
            const items = todoItems(data);
            if (items.length > 0)
                state.todos = items;
            state.todoCount += 1;
            break;
        }
        case 'step/start':
            state.stepCount += 1;
            break;
        case 'tool/call': {
            const action = formatAction(data ?? {});
            if (action !== null)
                state.action = action;
            break;
        }
        case 'assistant/message':
            accumulateUsage(state, data ?? {});
            break;
        case 'turn/end':
            armIdleTimer(state, cfg, write);
            break;
        case 'session/title': {
            const title = (data ?? {}).title;
            if (typeof title === 'string' && title.trim().length > 0)
                state.title = title.trim();
            break;
        }
        default:
            break; // step/end、tool/result、turn/start、session：仅记时间线
    }
    const item = { t: formatTime(event.time), type, desc: timelineDesc(type, data, state) };
    state.timeline.push(item);
    if (state.timeline.length > cfg.timelineMax) {
        state.timeline.splice(0, state.timeline.length - cfg.timelineMax); // 只留最近 N 条
    }
    write(state);
}
/** turn/end 后启动空闲定时器：超时无新事件 → 标记 finished 并保留文件。 */
function armIdleTimer(state, cfg, write) {
    if (state.idleTimer !== undefined)
        clearTimeout(state.idleTimer);
    state.idleTimer = setTimeout(() => {
        state.idleTimer = undefined;
        if (state.finished)
            return;
        state.finished = true;
        write(state);
    }, cfg.idleTimeoutMs);
}
/**
 * 挂载插件：订阅会话生命周期与事件流，输出进度 JSON。
 * @param ctx - Cordis 上下文（会话事件源）。
 * @param config - 插件配置（可选字段带默认值）。
 */
export function apply(ctx, config) {
    const logger = { warn: (message) => ctx.logger.warn(message) };
    const cfg = {
        outDir: config.outDir?.trim() || defaultOutDir(),
        idleTimeoutMs: config.idleTimeoutMs ?? 15000,
        timelineMax: config.timelineMax ?? 50,
        writeCurrent: config.writeCurrent ?? true,
    };
    const states = new Map();
    const write = (state) => {
        const json = buildProgressJson(state, cfg.timelineMax);
        writeJsonAtomic(state.filePath, json, logger);
        if (cfg.writeCurrent) {
            writeJsonAtomic(join(cfg.outDir, 'current.json'), json, logger);
        }
    };
    const ensureState = (session) => {
        const id = String(session.header.id);
        let state = states.get(id);
        if (state === undefined) {
            state = createState(session, cfg.outDir);
            states.set(id, state);
        }
        return state;
    };
    // 新会话开始 → 重置状态并创建进度文件
    ctx.on('session/created', (session) => {
        write(ensureState(session));
    });
    // 语义事件 → 推进状态并原子重写进度文件（chunk 噪音在此过滤）
    ctx.on('session/event', (session, event) => {
        const state = ensureState(session);
        handleEvent(state, event, cfg, write);
    });
    // 会话终态（进程退出/树销毁前触发）→ 标记 finished 并保留文件
    ctx.on('session/disposed', (session) => {
        const id = String(session.header.id);
        const state = states.get(id);
        if (state === undefined)
            return;
        if (state.idleTimer !== undefined) {
            clearTimeout(state.idleTimer);
            state.idleTimer = undefined;
        }
        if (!state.finished) {
            state.finished = true;
            write(state);
        }
        states.delete(id); // 内存态释放；进度文件保留供看板读取
    });
}
