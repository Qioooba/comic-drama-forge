import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'
import fs from 'fs'

// 应用版本号的「唯一事实源」= electron-app/package.json 的 version。
// 为什么是它：发版时 CI（.github/workflows/desktop-release.yml）先把 tag 版本写进
// electron-app/package.json，再由 pack_backend.js 触发 `npm run build` 重建前端
// —— 构建顺序决定了这里读到的一定是本次发布的版本。
// 为什么不在界面里硬编码：旧代码在 Sidebar 写死 "v1.0.0"，版本升到 1.2.0 后界面仍然
// 显示 1.0.0（用户实测反馈），属于典型的「双事实源漂移」。
function readAppVersion(): string {
  try {
    const pkg = JSON.parse(
      fs.readFileSync(path.resolve(__dirname, '../electron-app/package.json'), 'utf8')
    )
    return String(pkg.version || '').trim() || '0.0.0'
  } catch (e) {
    console.warn('[vite] 读取 electron-app/package.json 版本失败，界面将显示 v0.0.0：', e)
    return '0.0.0'
  }
}

const APP_VERSION = readAppVersion()

export default defineConfig({
  plugins: [react()],
  // 编译期常量替换：前端用 __APP_VERSION__ 引用，无需运行时请求
  define: {
    __APP_VERSION__: JSON.stringify(APP_VERSION),
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true
      }
    }
  },
  build: {
    outDir: '../app/static',
    emptyOutDir: true,
    rollupOptions: {
      input: './index.html',
    }
  }
})
