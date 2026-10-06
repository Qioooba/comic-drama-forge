/**
 * 交付域入口（导出 + 成品验收）。
 * 说明见 `features/audio/index.ts`：单文件单职责的域用 re-export 建入口，
 * 不搬动组件本体（搬动零收益、大 diff、且会让旧路径在搬迁窗口内静默失效）。
 */
export { OutputReviewTab } from '@/components/OutputReviewTab';