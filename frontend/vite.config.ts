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

// 端口唯一事实源与 Python 侧共享：app/ports.py 的 BACKEND_PORT / FRONTEND_PORT。
// 之所以在这里手工读根目录 .env 而不是靠 Vite 的 envDir：vite 只把 VITE_ 前缀
// 的变量注入客户端代码，而我们需要的是**构建期**的 dev-server 配置。
// 优先级：进程环境变量 > 仓库根 .env > ports.py 同名常量（下方 FALLBACK_*）。
//
// ⚠️ 两端必须成对改：dev server 把 /api 代理到后端端口，只改其一会导致
// 「页面能开、接口全 502 / ECONNREFUSED」这类半通状态。
const FALLBACK_BACKEND_PORT = 45871
const FALLBACK_FRONTEND_PORT = 47311

function readRootEnv(key: string): string {
  try {
    const raw = fs.readFileSync(path.resolve(__dirname, '../.env'), 'utf8')
    for (const line of raw.split(/\r?\n/)) {
      const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/)
      if (m && m[1] === key) {
        // 去掉行尾注释与两侧引号；值本身含 # 时不受影响（只砍 # 之前的部分）
        return m[2].replace(/\s+#.*$/, '').trim().replace(/^["']|["']$/g, '')
      }
    }
  } catch (e) {
    console.warn(`[vite] 读取根 .env 中的 ${key} 失败，使用内置默认值：`, e)
  }
  return ''
}

function resolvePort(key: string, fallback: number): number {
  const raw = process.env[key] || readRootEnv(key)
  const n = Number.parseInt(raw, 10)
  return Number.isFinite(n) && n > 0 && n < 65536 ? n : fallback
}

const BACKEND_PORT = resolvePort('APP_PORT', FALLBACK_BACKEND_PORT)
const FRONTEND_PORT = resolvePort('FRONTEND_PORT', FALLBACK_FRONTEND_PORT)

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
    port: FRONTEND_PORT,
    // strictPort：端口被占用时直接报错退出，而不是静默 +1 顺延。
    // 不加这条，dev server 会退回 47312/47313… 这类「刚好空着」的端口，
    // 而我们改成高位端口的意义（避开本机其它服务）就整个失效了，
    // 且这个偏移只打在终端里，很容易被忽略。
    strictPort: true,
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${BACKEND_PORT}`,
        changeOrigin: true
      }
    }
  },
  // `pnpm preview`（本地预览构建产物）的端口。独立于 dev server 是必要的：
  // 两者可能同时开着，各用各的端口才不会互撞。
  // 与 dev server 用同一个高位号是**故意的**——它们不会同时跑，且共用一个
  //  memorable 号省得记两个。若将来要同时开，改成另一个高位号即可。
  preview: {
    port: FRONTEND_PORT,
    strictPort: true,
    host: '127.0.0.1',
  },
  build: {
    outDir: '../app/static',
    emptyOutDir: true,
    rollupOptions: {
      input: './index.html',
    }
  }
})
