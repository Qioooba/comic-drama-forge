import React, { useEffect, useState } from 'react';
import { contractsApi } from '@/api/generated';
import { CONTRACT_VERSION, SPEC_HASH } from '@/api/generated';
import { t } from '@/i18n';

type GateState =
  | { kind: 'checking' }
  | { kind: 'ready' }
  | { kind: 'mismatch'; version: string; hash: string }
  | { kind: 'unavailable'; message: string };

/**
 * 启动期契约兼容闸门（ADR-0004 / 评估文档 P1-6）。
 *
 * 生成客户端带的是编译期契约版本与规格摘要；后端 `/api/contracts/version`
 * 返回运行时真实值。两者不一致时前端必须停止，而不是等到某个请求 404/字段缺失
 * 才发现。网络不可用按“后端未就绪”处理：显示告警但不把离线页面误判成契约过期。
 */
export function ApiCompatibilityGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<GateState>({ kind: 'checking' });

  useEffect(() => {
    let alive = true;
    contractsApi.getContractVersion()
      .then((remote) => {
        if (!alive) return;
        if (
          remote.contract_version !== CONTRACT_VERSION ||
          remote.spec_hash !== SPEC_HASH
        ) {
          setState({
            kind: 'mismatch',
            version: remote.contract_version || '',
            hash: remote.spec_hash || '',
          });
        } else {
          setState({ kind: 'ready' });
        }
      })
      .catch((error) => {
        if (!alive) return;
        setState({
          kind: 'unavailable',
          message: error instanceof Error ? error.message : String(error || ''),
        });
      });
    return () => { alive = false; };
  }, []);

  if (state.kind === 'checking') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-canvas text-ink-2" role="status" aria-live="polite">
        {t('contract.checking')}
      </div>
    );
  }
  if (state.kind === 'mismatch') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-canvas p-6">
        <div className="max-w-xl rounded-lg border border-danger bg-danger-subtle p-5 text-danger-strong" role="alert">
          <h1 className="text-lg font-semibold">{t('contract.mismatchTitle')}</h1>
          <p className="mt-2 text-sm">{t('contract.mismatchBody')}</p>
          <p className="mt-2 font-mono text-xs">
            client={CONTRACT_VERSION} / server={state.version}
            <br />
            client_hash={SPEC_HASH.slice(0, 12)} / server_hash={state.hash.slice(0, 12)}
          </p>
        </div>
      </div>
    );
  }
  if (state.kind === 'unavailable') {
    return (
      <div className="min-h-screen bg-canvas text-ink-1">
        <div className="border-b border-warning/50 bg-warning-subtle px-4 py-2 text-sm text-warning-strong" role="status">
          {t('contract.unavailable', { message: state.message })}
        </div>
        {children}
      </div>
    );
  }
  return <>{children}</>;
}

export default ApiCompatibilityGate;
