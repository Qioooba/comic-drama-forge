import React, { useState, useEffect } from 'react';
import { useApp } from '@/context/AppContext';

interface ServiceStatus {
  flask: 'ok' | 'error' | 'checking';
  comfyui: 'ok' | 'error' | 'checking' | 'unknown';
  lastCheck: string;
}

export function ServiceMonitor() {
  const { t } = useApp();
  const [status, setStatus] = useState<ServiceStatus>({
    flask: 'checking',
    comfyui: 'unknown',
    lastCheck: '',
  });

  useEffect(() => {
    const checkServices = async () => {
      const now = new Date().toLocaleTimeString('zh-CN');

      // Flask 与 ComfyUI 统一走后端 /api/status 一次查完。
      // 缺陷 D6：此前浏览器直接 fetch http://127.0.0.1:8188/system_stats，
      // 属于跨源请求（Origin=本应用端口 ≠ Host=127.0.0.1:8188），
      // ComfyUI 会以 "non matching host and origin" 返回 403，且每 30s 刷一次，
      // 在 ComfyUI 日志里形成高频告警。改为由后端代查即可彻底消除。
      try {
        const response = await fetch('/api/status');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data: any = await response.json().catch(() => ({}));
        const comfy = data?.comfyui || {};
        setStatus({
          flask: 'ok',
          comfyui: comfy.error ? 'error' : 'ok',
          lastCheck: now,
        });
      } catch {
        setStatus(prev => ({ ...prev, flask: 'error', lastCheck: now }));
      }
    };

    checkServices();
    const interval = setInterval(checkServices, 30000); // Check every 30 seconds

    return () => clearInterval(interval);
  }, []);

  if (status.flask === 'ok') return null;

  return (
    // z-toast：断线横幅是语义上最高优先级的告警，必须压过 AppShell 那条 z-drawer 的
    // 底部检视抽屉（≤1024px 时二者都贴视口底部，抽屉会把这条最该被看见的横幅完全盖住）。
    <div className="fixed bottom-4 right-4 z-toast">
      <div className={`flex items-center gap-2 px-4 py-2 rounded-lg border shadow-lg ${
        status.flask === 'error'
          ? 'border-danger/30 bg-danger-subtle text-danger-strong'
          : 'border-warning/30 bg-warning-subtle text-warning-strong'
      }`}>
        {/* 圆点跟随横幅的 -strong 文字色（bg-current），避免深色主题下变成暗色压暗色 */}
        <span className="w-2 h-2 rounded-full bg-current animate-pulse"></span>
        <span className="text-sm font-medium">
          {status.flask === 'error' ? t('common.serviceConnectFailed') : t('common.checking')}
        </span>
        {/* 保留原生：该按钮叠在 danger/warning 底色上，ghost 变体的 text-ink-2 /
            hover:bg-surface-2 会在语义色底上失去对比度 */}
        <button
          onClick={() => window.location.reload()}
          className="ml-2 px-2 py-1 bg-surface/20 rounded hover:bg-surface/30 active:opacity-90 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
        >
          {t('common.retry')}
        </button>
      </div>
    </div>
  );
}

