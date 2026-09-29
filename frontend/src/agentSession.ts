import type { AgentStep } from '@/types';

/** 对话消息（含结构化的总控执行轨迹 kind='run'） */
export interface ChatMsg {
  role: string;
  content?: string;
  timestamp?: string;
  kind?: 'run';
  steps?: AgentStep[];
  status?: string;
}

export interface AgentSession {
  /** 本次前端会话内的消息（含 run 轨迹；后端历史里没有结构化轨迹） */
  messages: ChatMsg[];
  /** 进行中的总控 job：切换菜单/收起面板后据此恢复轮询 */
  runningJobId: string | null;
  /** job 开始时间（epoch ms），用于执行耗时显示 */
  runningStartedAt: number | null;
}

/**
 * 总控面板的会话级缓存（模块级单例，SPA 内切换菜单不丢）。
 *
 * 背景：ChatPanel 挂在工作台页内，切到其它侧边栏菜单再回来时组件整体卸载——
 * 此前 messages 由后端历史重建（结构化 run 轨迹丢失）、进行中的 job 轮询直接死亡。
 * 这里把消息与 running job 存在模块级 Map（key=projectKey），重挂载时原样恢复
 * 并继续跟踪同一个 job；离开期间已完成的步骤由 job.steps 一次性补齐。
 *
 * ⚠️ 仅内存缓存：整页刷新后 messages 回退到后端历史（run 轨迹不留），runningJobId
 *    清空——job 本身仍在后端继续跑，不影响生产，只是前端不再展示过程。
 */
const sessions = new Map<string, AgentSession>();

export function getAgentSession(projectKey: string): AgentSession {
  let s = sessions.get(projectKey);
  if (!s) {
    s = { messages: [], runningJobId: null, runningStartedAt: null };
    sessions.set(projectKey, s);
  }
  return s;
}
