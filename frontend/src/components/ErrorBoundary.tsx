import React, { Component, ErrorInfo, ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { Button } from '@/components/ui';
import { AlertTriangle } from '@/components/ui/icons';
import { t } from '@/i18n';

/**
 * 「回首页」按钮。**必须**是独立函数组件：`ErrorBoundary` 本身是 class，
 * 拿不到 `useNavigate`（hook 只能在函数组件里调用）。
 *
 * 改造前这里写的是 `window.location.hash = '#/'` + `window.location.reload()`：
 * 直接改 hash 是**绕过 router** 的写法（hash 变了还得等 HashRouter 的 hashchange
 * 事件把路径同步过去，且与随后的 reload 存在竞态 —— reload 可能先执行）。
 * 现在改成 `navigate('/')`：路由同步切换（`/` 按路由表 301 到 `/projects`），
 * reload 保留 —— 它是「清掉内存里的坏状态」的既有行为，去掉会让错误边界可能点不动。
 */
function BackHomeButton(): JSX.Element {
  const navigate = useNavigate();
  return (
    <Button
      variant="secondary"
      onClick={() => {
        navigate('/', { replace: true });
        window.location.reload();
      }}
    >
      {t('error.backHome')}
    </Button>
  );
}

// 本组件是 class 组件，不能用 useApp() hook，故使用模块级 t()。
// 语言包由 AppProvider 在 App 层异步加载，ErrorBoundary 的渲染时机可能早于语言包就绪，
// 此时 t() 会回落成 key 本身（i18n/index.ts 的既有设计），属预期行为、不是 bug。

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
  onError?: (error: Error, errorInfo: ErrorInfo) => void;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  public state: State = {
    hasError: false,
    error: null,
  };

  public static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('ErrorBoundary caught an error:', error, errorInfo);
    this.props.onError?.(error, errorInfo);
  }

  public render() {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback;
      }

      return (
        <div className="min-h-screen flex items-center justify-center bg-surface-2">
          <div className="text-center p-8 bg-surface rounded-lg shadow-lg max-w-md mx-4">
            <div className="mb-4 flex justify-center">
              <AlertTriangle className="h-16 w-16 text-danger" />
            </div>
            <h2 className="text-xl font-bold text-ink-1 mb-2">
              {t('error.boundaryTitle')}
            </h2>
            <p className="text-ink-2 mb-4">
              {t('error.boundaryDesc')}
            </p>
            {import.meta.env.DEV && this.state.error && (
              <details className="text-left text-sm text-ink-2 mb-4">
                <summary className="cursor-pointer hover:text-ink-1">
                  {t('error.boundaryDetails')}
                </summary>
                <pre className="mt-2 p-3 bg-surface-2 rounded overflow-auto text-xs">
                  {this.state.error.toString()}
                </pre>
              </details>
            )}
            <div className="flex gap-3 justify-center">
              <Button variant="brand" onClick={() => window.location.reload()}>
                {t('error.reload')}
              </Button>
              <BackHomeButton />
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
