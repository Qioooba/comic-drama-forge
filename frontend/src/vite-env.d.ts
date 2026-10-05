/// <reference types="vite/client" />

/**
 * 构建期注入的应用版本号（见 vite.config.ts 的 define）。
 * 事实源：electron-app/package.json 的 version。
 */
declare const __APP_VERSION__: string;
