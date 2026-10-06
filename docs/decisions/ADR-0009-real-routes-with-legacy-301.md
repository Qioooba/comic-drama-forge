# ADR-0009：前端换真路由 + 旧 hash 301 退役层

- 状态：**决策已采纳，路由层与布局/features 拆分已在当前工作树落地；运行时验证未执行**（2026-10-07）
- 决策者：项目维护者
- 相关：`frontend/src/App.tsx:28-41`、`frontend/src/router.tsx`、`frontend/src/routes/**`

> ## ⚠️ 实施状态声明（2026-10-07 收口审核补记）
>
> **当前工作树静态核对**：
> - `frontend/src/router.tsx`、`routes/legacyRedirect.tsx`、`routes/workbenchTabs.tsx` 已存在，并由 `App.tsx` 消费；
> - `layouts/AppShell.tsx`、`layouts/EpisodeContextBar.tsx` 与多个 `features/*` 目录已存在；
> - 本轮未运行浏览器、`tsc` 或构建，因此只确认源码接线，不宣称运行时验收。

## 背景

前端此前是**手写 hash 路由**（`App.tsx:28-41` 的 `parseRoute()`）：

```ts
function parseRoute(): { section: string; projectKey: string | null } {
  const hash = window.location.hash;
  let path = hash.replace('#', '') || '/';
  if (path.startsWith('/#')) { path = path.slice(1); }          // 兼容 #/#/foo
  const params = new URLSearchParams(path.split('?')[1] || '');
  const projectKey = params.get('p');                          // ← 项目上下文走 query
  const route = path.split('?')[0].replace(/^\//, '');
  const section = projectKey ? 'project-workbench'
                             : (route && SECTIONS[route] ? route : 'projects');
  return { section, projectKey };
}
```

这段 14 行代码承担了路由的全部工作，代价是：

1. **没有路由表**。可导航的页面藏在 `SECTIONS` 常量里，不是数据；
2. **无法嵌套路由**。工作台的 7 个 Tab 是组件内 state，不是路由——
   于是 Tab 状态不进 URL，**刷新即丢失、无法分享链接**；
3. **兜底是静默的**。`SECTIONS[route]` 找不到就落回 `projects`。
   一个拼错的链接**不报错、不报警**，只是「打开后看到的是另一个页面」。

第 3 点是本 ADR 的核心动机，不是「路由写法不够现代」。

## 决策

换 `react-router-dom` 真路由（已安装，无需新增依赖），并保留**旧 hash 301 退役层**。

### 关键决策：为什么不能一刀切删旧路由

因为**死链无人报警**。

这是前端 hash 路由与后端 URL 最本质的差别：

| | 服务端路由 | 前端 hash 路由 |
|---|---|---|
| 链接失效时的表现 | HTTP 404，**监控能报警** | 落回默认页，**静默无感** |
| 谁能看到错误 | 运维 / 监控 / 用户 | 只有用户，且用户以为「进去了」 |

所以删掉旧路由解析不会让任何人发现问题——**它只会让已经收藏了
`#/project?p=xxx` 的用户，在某天点进去之后莫名其妙地停在首页**。
而这些链接可能出现在：用户自己的浏览器书签、上一版交接文档、
工单/聊天记录、微信里发给同事的链接。**死链的半衰期比代码长得多。**

因此保留退役层：

1. 旧 hash 路径仍被解析；
2. 命中即 `navigate(to, { replace: true })` —— **一次性跳转**，
   地址栏变成新路径（等价于 HTTP 301：不留在旧地址上反复重定向）；
3. **query 与 hash 全部保留**（见下）。

### 关键决策：query / hash 必须原样保留

旧路由用 `?p=` 传项目上下文（`projectKey`）。新路由是 `/projects/:projectKey/...`。

**query 丢了就是丢上下文**：用户点着点着回到总览页，突然不知道自己在哪个项目。
这是「看起来能用但很烦」的典型退化，且不会报错——所以只能靠规则守住：

- 退役层跳新路径时**必须把原 query string 完整带上**；
- 旧 hash 里若带 `#` 锚点（如 `#/project?p=x#shot-3`），锚点**必须保留**，
  否则「定位到第 3 镜」变成「定位到项目首页」；
- 这两条写成规则而非「记得注意」，因为靠人记的规矩守不住（见 ADR-0011）。

退役清单要**显式列出**（哪些旧路径映射到哪个新路径），
并由 `npx tsc --noEmit` + 路由表类型兜底，不靠人肉核对。

## 为什么不选另一条路

**一刀切删掉旧 hash 解析。** 代码最干净。但死链静默失效、无人报警，
且旧链接的半衰期远长于本次改造——见上。这是本 ADR 存在的理由。

**继续用 hash 路由，只把解析写规范些。** 没有死链问题（地址不变）。
但换来的仍是「没有路由表、无法嵌套、Tab 状态不进 URL」，
且 `parseRoute()` 的静默兜底问题依然在。既然要动这块，不如换成能报警、能嵌套的方案。

**换 `hashHistory`（HashRouter）。** 地址里保留 `#/`，看似无需退役层。
但①「URL 仍不可分享给外部系统」的根因没解决；②仍然没有嵌套路由；
③`HashRouter` 在 Electron 打包 + `file://` 协议下的行为比 `BrowserRouter` 更差。
**换路由器不等于换路由模型**，而本轮要换的正是路由模型。

**改用 query-only 状态（`?tab=qc`）而不是子路由。** 改动更小。
但工作台的左右栏、镜头检查器、候选对比 dialog 都需要自己的可分享状态，
query 会被塞满且难维护。7 个 Tab 已经是需要嵌套的形态了。

## 后果

**正面**

- 可导航页面变成**路由表数据**，不是组件内 state；
- Tab / 选中镜头等状态进 URL，**可刷新、可分享、可前进后退**；
- 旧链接有人接住，不再静默死链；
- `react-router-dom` 已在 `package.json`，无新增依赖。

**负面 / 代价**

- **两套路由并存期**：旧 hash 解析 + 新路由表。退役清单要显式维护，
  且必须防「同一页面有两个入口」的漂移。
- `parseRoute()` 里那条 `#/#/foo` 兼容归一化逻辑**要保留到退役期结束**——
  删早了会让更老的一批链接死掉。
- 真路由引入 `BrowserRouter` 在 Electron 打包下的 base path 处理问题
  （子路径部署时需要 `basename`）。
- **路由层从 14 行变成一张表 + 退役层**：净代码量增加。换来的是可报警性。

**遗留**

- 退役层是**永久保留**还是设「N 个版本后删除」？本轮决定**保留**，
  因为无法判断用户书签的分布。删除条件应基于数据（访问日志里旧路径归零），
  而本项目单机部署、没有访问日志。**这是一个已知的长期成本，不是遗漏。**

## 关联决策

- ADR-0005 Job / Attempt（前端的任务视图消费 `/api/jobs`）
- ADR-0010 TanStack Query（路由切页是数据重新获取的主场景，`keepPreviousData` 的关键用例）
- ADR-0011 设计规范文档化 + 自动化校验
