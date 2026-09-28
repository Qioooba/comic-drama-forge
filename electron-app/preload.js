'use strict';
// preload：通过 contextBridge 暴露最小 IPC 面。
// 不暴露 node/electron 本体，只暴露明确的 promise 方法。

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('mjscxt', {
  // 后端生命周期
  backend: {
    start: () => ipcRenderer.invoke('backend:start'),
    stop: () => ipcRenderer.invoke('backend:stop'),
    restart: () => ipcRenderer.invoke('backend:restart'),
    // 取日志环形缓冲（最近 n 行，默认 100）
    log: (n = 100) => ipcRenderer.invoke('backend:log', n),
    // running/port/pid/reused
    status: () => ipcRenderer.invoke('backend:status'),
  },
  // 配置
  config: {
    // 目录选择器 → 设置项目根（校验 app/serve.py 存在）
    chooseProjectRoot: () => ipcRenderer.invoke('config:chooseProjectRoot'),
    setProjectRoot: (root) => ipcRenderer.invoke('config:setProjectRoot', root),
    get: () => ipcRenderer.invoke('config:get'),
  },
});
