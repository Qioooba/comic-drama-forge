/**
 * TanStack Query 客户端（ADR-0010）。
 *
 * 为什么把轮询收进这一处
 * ----------------------
 * 改造前工作台里手写 `useState` + `setInterval`，同一套「取数 / 缓存 / 轮询 / 重试 / 三态」
 * 被逐点重写了一遍又一遍，5 个容易写错的点在每个轮询点重犯（ADR-0010 §背景）。
 * 这里把默认策略定死一次，组件只声明「我要什么 key」和「多久刷新一次」。
 *
 * 默认值为什么这么定
 * ------------------
 * - `staleTime` 8s：绝大多数工作台数据在一次人工操作周期内不会变。默认 0 会让
 *   每个挂载组件都立刻重拉一遍 —— 这正是「切个 Tab 回来重新请求」的根因。
 *   轮询类 query 会自己覆盖成 0（它们要的就是「一直新」）。
 * - `refetchInterval` 默认**关掉**（`false`）：轮询是**按需**的，不是全局默认行为。
 *   开了默认值就会让「打开一个不轮询的页面」也一直打后端 —— 这是把「轮询」
 *   从一种显式选择变成了默认行为，恰好是 ADR-0010 要消除的隐式行为。
 * - `retry` 只对 GET 重试 1 次：读失败重试有意义；再多次会把后端的真实错误
 *   （409/403 这类契约错误）拖成几十秒的等待。写操作**不重试** ——
 *   重复提交 POST 等于重复「采用/批准」，比失败更糟。
 *
 * 失效策略：写操作一律 `invalidateQueries`（重新取真值），
 * **不做**乐观更新 —— 采用/批准是有审计留痕的事实，前端猜一个中间态再回滚，
 * 等于让界面短暂地显示一个「从未发生过」的决策状态。
 */
import { QueryClient } from '@tanstack/react-query';

/** 默认 stale 窗口：够覆盖一次「看一眼→点一下」的间隔，又不至于一直显示旧数据。 */
export const DEFAULT_STALE_TIME = 8_000;

/**
 * 模块级单例。
 *
 * 必须是**模块级**而不是组件内 `useState(() => new QueryClient())`：
 * 后者在 Provider 重挂时会产生第二个 client，缓存分裂成两份，
 * 跨 Tab 共享就失效了 —— 而共享正是这次改造的主要收益。
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: DEFAULT_STALE_TIME,
      refetchInterval: false,
      // 关掉窗口聚焦时的整页重取：工作台自身已有显式轮询，
      // 与 focus 重取叠加会在切回窗口时打两遍后端。
      //
      // ⚠️ 这里原本是 `true` —— 与上面这段注释承诺的行为**相反**（注释描述的
      //    意图是关闭，实现却是打开）。已按注释改成 `false`：工作台数据靠
      //    各查询自己的轮询节奏刷新，不需要再叠一层隐式的全局重取。
      //    也不会因此显示陈旧数据：`staleTime` 为 0，组件重新挂载（切 tab 回来）
      //    时本来就会重取。
      refetchOnWindowFocus: false,
      // 只读接口失败重试 1 次；查询函数里抛的业务错误（409/403）不该被重试掩盖
      retry: 1,
      // 断线重连后重取真值（与 focus 不同：网络恢复后必须重新确认状态）。
      refetchOnReconnect: true,
      gcTime: 5 * 60_000,
    },
    mutations: {
      // 写操作不自动重试：「采用」「批准」重复提交等于重复决策
      retry: 0,
    },
  },
});

/**
 * 丢弃某个项目的全部缓存。
 *
 * 切项目时必须调：query key 里带了 projectKey 的话旧数据不会被误读，
 * 但它会一直占着内存直到 gcTime 到期。这里显式清理，语义与旧的
 * `useEffect(() => { setState(initial) }, [projectKey])` 一致。
 */
export function clearProjectCache(projectKey: string): void {
  queryClient.removeQueries({ queryKey: ['project', projectKey] });
}

export default queryClient;