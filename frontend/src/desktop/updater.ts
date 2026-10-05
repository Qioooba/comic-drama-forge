// ============================================
// 桌面版（Electron）更新桥接层
// ============================================
// 为什么单独抽一层：
//   1. window.mjscxt 仅由 electron-app/preload.js 经 contextBridge 暴露，
//      浏览器版 / PyInstaller 单文件版**没有**这个全局对象 —— 直接引用会崩；
//   2. 各页面统一从本模块取 updater，非桌面环境拿到 null，UI 侧据此隐藏入口，
//      不需要每个调用点各自写 typeof 判断；
//   3. 类型收口：IPC 返回的更新结果结构只在这里定义一次。
//
// 与后端 updater.js（electron-app/updater.js）的对应关系：
//   checkForUpdates      → ipc 'updater:check'      （纯数据，不弹窗）
//   downloadAndUnpackResources / applyFullPortable + stop/startBackend
//                          → ipc 'updater:applyResource' / 'updater:applyFull'
// 后两者由主进程完成「下载 + SHA256 校验 + 重启后端 / 自重启」，前端只管发起与展示进度。
export interface DesktopUpdateInfo {
  ok: boolean;
  update?: boolean;
  latest?: string;
  current?: string;
  releaseName?: string;
  needFull?: boolean;
  needResource?: boolean;
  assets?: { name: string; url: string }[];
  error?: string;
}

/** 资源增量更新结果（主进程已重启后端） */
export interface DesktopUpdateApplyResult {
  ok: boolean;
  applied?: 'resource' | 'full';
  latest?: string;
  error?: string;
}

export interface DesktopUpdater {
  /** 拉取 GitHub 最新发布并比较版本；不弹窗、纯数据 */
  check: () => Promise<DesktopUpdateInfo>;
  /** 资源增量：下载 zip + 校验 + 覆盖镜像 + 重启后端（同步等待完成） */
  applyResource: () => Promise<DesktopUpdateApplyResult>;
  /** 整包：下载 portable exe + 校验 + 用户确认后 spawn bootstrapper + 自重启（可能直接退出本进程） */
  applyFull: () => Promise<DesktopUpdateApplyResult>;
}

/**
 * 是否运行在 Electron 桌面壳里（且 preload 已注入 updater 通道）。
 * 判断依据是 window.mjscxt?.updater 存在 —— 只有 electron-app/preload.js
 * 暴露过它，浏览器 / exe 版返回 null，UI 据此完全不渲染更新入口。
 */
export function getDesktopUpdater(): DesktopUpdater | null {
  const w = window as unknown as { mjscxt?: { updater?: DesktopUpdater } };
  return w.mjscxt?.updater ?? null;
}

/** 供 UI 直接判断「能否显示更新入口」 */
export function hasDesktopUpdater(): boolean {
  return getDesktopUpdater() != null;
}
