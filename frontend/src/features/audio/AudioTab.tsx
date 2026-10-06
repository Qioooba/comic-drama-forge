import React from 'react';
import { useApp } from '@/context/AppContext';
import { AudioTab as LegacyAudioTab } from '@/components/AudioTab';

/**
 * 声音页入口：保留旧逐句 TTS / 混音界面，但先明确标注两条链路的语义。
 * 正式托管链路 = H3 原生音轨 + tts_pre 参考音色；逐句 TTS + 混音仅作为
 * 手工回滚/返工路径，不再让用户误以为它是默认成片链路。
 */
export function AudioTab({ projectKey }: { projectKey: string }) {
  const { t } = useApp();
  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-info/40 bg-info-subtle p-3 text-sm text-info-strong" role="note">
        <div className="font-semibold mb-1">{t('audio.workflowNoticeTitle')}</div>
        <div className="space-y-1 text-xs sm:text-sm">
          <div>• {t('audio.workflowNoticeFormal')}</div>
          <div>• {t('audio.workflowNoticeManual')}</div>
        </div>
        <p className="mt-2 text-xs text-ink-2">{t('audio.workflowNoticeHint')}</p>
      </div>
      <LegacyAudioTab projectKey={projectKey} />
    </div>
  );
}
