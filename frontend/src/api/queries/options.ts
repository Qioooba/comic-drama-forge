/**
 * 轮询与「保留旧数据」的统一选项（ADR-0010）。
 *
 * 这一文件是 ADR-0010 那条「两者是一套，不可以只取其一」规则的**唯一实现点**。
 * 把它做成一个函数而不是让每个 query 各写一遍，是因为漏掉 stale 标识的后果
 * 比闪一下空态更危险（用户会在上一集的数据上点操作）。
 */
import { keepPreviousData } from '@tanstack/react-query';

/** 工作台轮询的默认节奏。3s = 用户在前台等一张图的观感；详见各调用点的注释。 */
export const POLL_FAST = 3_000;
/** 慢轮询：空闲兜底，等新任务起来。 */
export const POLL_SLOW = 12_000;
/** 分镜画布：整步 20+ 镜、每镜 2-3 分钟，5s 足够接近实时又不压后端。 */
export const POLL_CANVAS = 5_000;

/**
 * 「保留旧数据」选项组。
 *
 * ⚠️ 刻意**不**用 `Pick<UseQueryOptions<T>, ...>` 泛型返回：那会让 `T` 在调用点
 *    反向推断成 `unknown`，进而让 `placeholderData` / `staleTime` 与实际查询类型
 *    不兼容而编译失败。这里用一个与 T 无关的具体类型，`keepPreviousData` 自身是
 *    泛型函数，因此可以赋给任意 `TQueryFnData`。
 */
export interface KeepPreviousOptions {
  /** 切到新 key 时保留上一页数据，不塌成骨架屏 */
  placeholderData: typeof keepPreviousData;
  /** 轮询类 query 要的就是「一直新」，故压到 0 */
  staleTime: number;
  refetchInterval: number | false;
  /** 保留旧数据时切回窗口应立刻校正，不等下一个间隔 */
  refetchOnWindowFocus: boolean;
}

/**
 * 换 key 时的保留旧数据选项。
 *
 * 为什么必须有 stale 标识
 * ----------------------
 * `keepPreviousData` 期间屏幕上显示的是**上一集/上一镜的数据**。
 * 不同时给出 `isPreviousData`，用户就会以为那是当前这一集的内容并直接操作
 * —— 这比闪一下空态危险得多。所以本文件只提供这一份配置，
 * 让「配了 keepPreviousData 却忘了标 stale」在代码 review 里一眼可见。
 *
 * @param refetchInterval 轮询间隔；传 `false` 表示不轮询（纯按需取数）。
 */
export function keepPreviousOptions(refetchInterval: number | false = false): KeepPreviousOptions {
  return {
    placeholderData: keepPreviousData,
    staleTime: 0,
    refetchInterval,
    refetchOnWindowFocus: true,
  };
}