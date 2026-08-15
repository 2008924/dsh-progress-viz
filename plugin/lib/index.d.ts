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
import type { Context } from '@deepseek-ai/cordis';
import Schema from '@deepseek-ai/schemastery';
import type { ProgressVizConfig } from './types.js';
/** 稳定插件名（loader patch 的 row id 与此一致）。 */
export declare const name = "progress-viz";
/**
 * 本插件不注入业务服务：只通过 ctx.on 订阅会话生命周期与事件流
 * （session/created、session/event、session/disposed）。
 */
export declare const inject: string[];
/** 插件配置（全部可选带默认值：零配置即可挂载）。 */
export declare const Config: Schema<ProgressVizConfig>;
/**
 * 挂载插件：订阅会话生命周期与事件流，输出进度 JSON。
 * @param ctx - Cordis 上下文（会话事件源）。
 * @param config - 插件配置（可选字段带默认值）。
 */
export declare function apply(ctx: Context, config: ProgressVizConfig): void;
