# ADR-0010：引入 TanStack Query 收敛轮询

- 状态：**已采纳并已实施**（决策 2026-10-07；实施 2026-10-07，见下方「实施结果」）
- 决策者：项目维护者
- 相关：`frontend/src/api/queryClient.ts`、`frontend/src/api/queries/**`、`frontend/src/pages/ProjectWorkbenchPage.tsx`

> ## 实施结果（2026-10-07，收口审核确认）
>
> 原「⚠️ 实施状态声明」已删除 —— 它记录的是**依赖装上但未接线**的中间态，
> 而该状态已不存在。留下它会制造相反方向的误导。
>
> | 本 ADR 提到的路径 | 当时实测 | 现在 |
> |---|---|---|
> | `frontend/src/api/queryClient.ts` | 不存在 | **69 行**，模块级单例 + `clearProjectCache` |
> | `frontend/src/api/queries/**` | 空目录 | **10 个文件 650 行**（keys / options / assets / autopilot / storyboard / jobs / upscale / productionFacts / errors / index） |
> | `@tanstack/react-query` | 声明但零引用 | **11 处引用，跨 11 个文件** |
> | 工作台 `setInterval` 轮询 | 7 处真实调用（另有 4 处 `ReturnType<typeof setInterval>` 类型引用） | **1 处**，且不是取数轮询 |
>
> ### 唯一保留的那一处 `setInterval`
>
> `AgentTrace` 里 1s 一次的「重渲染节拍」：数据由 query 提供，定时器不发任何请求，
> 只让 `Date.now()` 的差值每秒重算一次。把它塞进 query 会**多发请求**，
> 与本次改造的目的相反。ADR 收敛的是「取数」，不是「所有定时器」。
>
> ### 仍然手写轮询的部分（如实登记）
>
> `components/AutopilotPanel.tsx`、`pages/LogsPage.tsx`、`components/ServiceMonitor.tsx`、
> `components/layout/Navbar.tsx` 四处不在本轮可写清单内，仍是手写轮询。
> **两种写法仍在一段时间内并存** —— 与本 ADR「后果·负面」一节的自述一致。
>
> ### 两处与本文措辞不符的实现细节（以实现为准）
>
> 1. **`isPreviousData` 在 v5 已移除。** 本文「关键设计」一节用它举例 stale 标识，
>    但 `@tanstack/react-query@5.104.1` 中该字段不存在，同一语义由
>    **`isPlaceholderData`** 承担。代码里用的是后者。
> 2. **Provider 挂在工作台壳内，不在 `App.tsx` / `main.tsx`。**
>    这两个文件本轮禁改。工作台子树之外没有任何 query，
>    因此它是作用域**恰好正确**的边界；client 仍是模块级单例，缓存不会分裂。

## 背景

轮询逻辑此前是**手写 `useState` + `setInterval`**，散落在工作台各处。
`ProjectWorkbenchPage.tsx`（5.1k 行）里 `useState` / `useRef` / `setInterval`
合计 161 处；§4.3 记录的 `StoryboardTab` 单个内联组件（约 780 行）就有
**22 个 `useState` + 4 个轮询 ref**。

每一处轮询都是同一套逻辑的又一次手工实现，而这套逻辑有 5 个容易写错的点：

1. **卸载后 timer 不清** —— 组件卸载后 interval 仍在跑，持续 `setState`；
2. **重挂载就重拉** —— 切个 Tab 回来，重新请求一遍；
3. **无请求仲裁** —— 慢响应覆盖快响应（竞态）。切集时先发的请求后到，
   界面回退到旧集的数据，且**不报错**；
4. **三态各写一遍** —— loading / error / retry 在每个轮询点重写，
   每处的空态文案都不一样；
5. **重复打后端** —— 多个组件轮询**同一个资源**，各自发请求。

第 3 点最隐蔽：它产生的 bug 是「数据偶尔不对」，不报错、无日志、难复现。

## 决策

引入 `@tanstack/react-query`（`queryClient.ts` + `api/queries/**`），
把「取数 / 缓存 / 轮询 / 重试 / 三态」收敛到一处，组件只声明
「我要什么 key」和「多久刷新一次」。

- 轮询 = `refetchInterval`，随组件生命周期自动启停；
- 重复资源按 query key 共享缓存，**同一时刻只有一份请求**；
- 请求竞态由库的 stale-while-revalidate 语义仲裁。

### 关键设计：`keepPreviousData` 语义

**`keepPreviousData: true` = 切到新 key 时，保留上一页的数据，直到新数据到达。**

不这么做的话，切集 / 切页的过程是：

```
新 key → 数据缓存未命中 → loading=true → 组件渲染空态 → 新数据到达 → 填充
```

界面上会**先塌成骨架屏 / 空列表，再填内容**。用户看到的是「列表清空了」，
而不是「正在加载下一集」。两个后果：

- **布局跳动**（内容高度从 0 跳到 N 行，把下方所有内容顶下去）；
- **误读为数据丢失** —— 用户很可能以为操作错了，于是重复点击。

`keepPreviousData` 把它变成：

```
新 key → 缓存未命中但有 previousData → 继续渲染旧数据（可标 stale 态）→ 新数据到达 → 替换
```

内容高度不变，切换是「就地更新」而不是「清空重填」。

**代价要说清楚**：`keepPreviousData` 期间显示的是**上一集的数据**。
所以必须同时提供 stale 标识（`isPlaceholderData` / `isFetching`），
否则用户会在旧数据上点了操作——**这比闪一下空态更危险**。
这两者是一套，不可以只取其一。

> ⚠️ 本文早期版本写作 `isPreviousData`。该字段在 `@tanstack/react-query@5`
> **已移除**，同语义由 `isPlaceholderData` 承担。代码里用的是后者；
> 下方「现状 / 自研 hook / 收益」几节仍保留 `isPreviousData` 字样，
> 那是在陈述被否决的备选方案原文，不是现行指引。

轮询的场景（任务进度、GPU 状态）**不开** `keepPreviousData`：
轮询不换 key，`keepPreviousData` 无意义，且会掩盖「进度停了」。

## 为什么不选另一条路

**继续手写 `useState` + `setInterval`（现状）。** 零依赖。但上面 5 个坑会
在**每一个新的轮询点**重犯一遍，而项目有 161 处。这是「靠人记住的规矩守不住」
的典型：不是不想写对，是写对的成本高于写错的成本。

**自研一个 `usePolling` hook。** 比裸 `setInterval` 好（能集中处理清理），
但**缓存、竞态仲裁、跨组件共享这三件事它都得自己写一遍**——
写出来的东西就是 TanStack Query 的一个劣化子集。而且它是项目私有抽象，
将来要迁的时候全部要重写。

**Redux / Zustand + 手写请求层。** 全局状态库解决的是「跨页面共享状态」，
而轮询的痛点是**服务端状态**——它的生命周期、缓存、失效规则与 UI 状态完全不同。
把服务端状态塞进 Redux 是经典的错配（会产生「缓存与真值不一致」的一整类 bug）。

**SWR。** 与 TanStack Query 同源，能力接近。选 TanStack Query 的原因是
**`keepPreviousData` 与 stale-while-revalidate 是一等公民**，
且缓存失效（invalidate）在「提交后刷新相关数据」这个高频场景上语义更清楚。

## 后果

**正面**

- 轮询的启停、清理、竞态、重试由一处负责，不再逐点重写；
- 相同资源共享缓存，减少重复请求；
- 切换体验变成「就地更新」，不再闪空态；
- `isFetching` / `isPreviousData` / `isError` 三态现成可用。

**负面 / 代价**

- **多一个依赖**，且引入「服务端状态」的心智模型——
  团队需要理解 query key 设计（key 写错 = 缓存串味，且不报错）。
- `keepPreviousData` 的 stale 期需要显式做视觉标识，否则会**在旧数据上误操作**
  （见上）。
- 迁移是**逐点**的，不能一刀切（协调书 R5：既有大文件本轮只做搬迁）。
  本轮不迁完的部分仍走手写轮询，**两种写法在一段时间内并存**。

## 关联决策

- ADR-0009 前端真路由（路由切页是数据重新获取的主场景，`keepPreviousData` 的关键用例）
- ADR-0004 契约优先（query key 消费生成客户端的强类型响应）
- ADR-0005 Job / Attempt（任务进度轮询消费 `/api/jobs`）