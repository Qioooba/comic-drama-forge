import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useApp } from '@/context/AppContext';
import { projectsApi, novelsApi } from '@/api/client';
import { workbenchPath } from '@/routes/workbenchTabs';
import { Button, Input, Modal, Badge, ConfirmDialog, Select, Skeleton, EmptyState, ErrorState } from '@/components/ui';
import { AlertTriangle, Check, Clapperboard, FileText, FolderOpen, ImageIcon, Pencil, Plus, Trash2 } from '@/components/ui/icons';
import { useToast } from '@/components/ui/toast';
import type { Project, Novel } from '@/types';

// 风格库（61 项可视化风格 + 8 画幅预设）自 2026-10-07 起改为消费 GET /api/styles：
// 后端 app/style_catalog.json 是唯一事实源，页面不再内联 61 条中文文案，
// 前端改了后端立刻知道（此前两边各写一份，是本项目最易漂移的一处）。
// 缩略图仍在前端 import（Flask 只挂了 /assets 路由，61 张图不搬走后端），
// style_id -> 图片 URL 的映射见 features/style/thumbnails.ts。
import { StyleGallery, useStyles, ASPECT_LABEL_KEYS, CUSTOM_STYLE, DEFAULT_ASPECT_VALUE } from '@/features/style';
import type { StyleCatFilter } from '@/features/style';

type NovelSource = 'upload' | 'existing';

const DEFAULT_CONFIG = {
  // 默认创作风格：后端数据值（非 UI 文案），保持原字面值「3D动漫渲染」不变，
  // 仅以 \u 转义书写，避免源码里出现 CJK（i18n 扫描要求本文件零中文）。
  style: '3D\u52a8\u6f2b\u6e32\u67d3',
  episodes: 10,
  shots_per_episode: 12,
  resolution: '768p_vertical',
  aspect_ratio: '16:9 \u6a2a\u5c4f',
  fps: 24,
  duration_per_shot: 5,
  qc_enabled: true,
  episode_duration_sec: 60,
  target_shots: 12,
  voice_map: {},
};

// 新建项目风格库（缩略图卡片 + 分类筛选 + 自定义）已搬出本文件：
//   - 61 项风格的文案 / 分类 / 默认画幅 / 关键词 → GET /api/styles（后端唯一事实源）
//   - 分类标签与卡片网格 → features/style/StyleGallery.tsx
//   - 8 项画幅预设 → 同上（value 与 i18n key 映射见 features/style/aspectPresets.ts）
// 选中值仍是**旧自由文本字面值**（config.style 过去仍存中文自由文本，改存 style_id
// 会打断既有生成链路）；style_id 的用途是稳定标识与历史对照。

// 视频生成方式（写入 config.video_mode）。
// ⚠️ 2026-10-02 修复：后端 `config.norm_video_mode` 已**只保留「整集一次生成」**
//   （per_shot / keyframe 废弃，一律归一成 episode，见 config.py:384）。
//   这里原先仍列 3 项且注释自称「与后端 config.VIDEO_MODES 一致」——与事实不符，
//   用户选了会被后端静默归一。现与后端同源收敛为单值。
const VIDEO_MODE_OPTIONS: { value: string; labelKey: string; descKey: string }[] = [
  { value: 'episode', labelKey: 'project.videoModeEpisode', descKey: 'project.videoModeEpisodeDesc' },
];

const ACCEPT_EXTS = '.txt,.docx,.pdf,.epub,.md';

export function ProjectsPage() {
  const { t } = useApp();
  const toast = useToast();
  const navigate = useNavigate();
  const [projects, setProjects] = useState<Project[]>([]);
  const [novels, setNovels] = useState<Novel[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');

  // --- 新建项目弹窗（上传小说 + 选已有小说 二合一）---
  const [showNewProject, setShowNewProject] = useState(false);
  const [source, setSource] = useState<NovelSource>('upload');
  const [projectName, setProjectName] = useState('');
  const [selectedNovel, setSelectedNovel] = useState('');
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState('');
  // 风格选择：风格库卡片（缩略图 + 分类筛选）+ 自定义输入。CUSTOM_STYLE 时用自定义值。
  // 目录来自 /api/styles（后端单一事实源）；端点未就绪时留空，提交时回落默认风格。
  const {
    styles: catalogStyles, counts: styleCounts, aspectPresets,
    defaultStyleId, loading: stylesLoading, error: stylesError,
  } = useStyles();
  const [stylePreset, setStylePreset] = useState('');
  const [styleCat, setStyleCat] = useState<StyleCatFilter>('all');
  const [customStyle, setCustomStyle] = useState('');
  // 默认风格：优先后端 default_style_id 对应的 value（与迁移前 STYLE_LIBRARY[0].value 同源），
  // 目录未到时用首项兜底。
  const defaultStyleValue = useMemo(
    () => catalogStyles.find((s) => s.style_id === defaultStyleId)?.value || catalogStyles[0]?.value || '',
    [catalogStyles, defaultStyleId],
  );
  // 画面比例（视频/分镜画幅）：与风格一起在新建入口统一设置
  // 默认 16:9 横屏（2026-09-28 由 9:16 翻转，与后端 style_kit.DEFAULT_RATIO 一致）。
  const [aspectRatio, setAspectRatio] = useState(DEFAULT_ASPECT_VALUE);
  // 视频生成方式（项目级，写入 config.video_mode）：默认整集一次生成
  const [videoMode, setVideoMode] = useState<string>('episode');
  const fileInputRef = useRef<HTMLInputElement>(null);

  // --- 编辑项目弹窗 ---
  const [editingProject, setEditingProject] = useState<Project | null>(null);
  const [editName, setEditName] = useState('');
  const [savingEdit, setSavingEdit] = useState(false);
  const [editError, setEditError] = useState('');

  // --- 删除项目弹窗 ---
  const [deletingProject, setDeletingProject] = useState<Project | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState('');

  // --- 项目封面（生成中按项目 id 记忆；coverUrls 存生成后的缓存击穿 URL） ---
  const [coverBusy, setCoverBusy] = useState('');
  const [coverUrls, setCoverUrls] = useState<Record<string, string>>({});

  const reload = async () => {
    const [p, n] = await Promise.all([
      projectsApi.list().then(d => d.projects || []).catch(() => [] as Project[]),
      novelsApi.list().then(d => d.novels || []).catch(() => [] as Novel[]),
    ]);
    setProjects(p);
    setNovels(n);
    return { p, n };
  };

  useEffect(() => {
    reload()
      .catch((e) => setLoadError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  const resetForm = () => {
    setSource('upload');
    setProjectName('');
    setSelectedNovel('');
    setPendingFile(null);
    setDragOver(false);
    setFormError('');
    setStylePreset(defaultStyleValue);
    setStyleCat('all');
    setCustomStyle('');
    setAspectRatio(DEFAULT_ASPECT_VALUE);
    setVideoMode('episode');
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const openModal = () => {
    resetForm();
    setShowNewProject(true);
  };

  const closeModal = () => {
    if (submitting) return;
    setShowNewProject(false);
    resetForm();
  };

  const pickFile = (file: File | undefined | null) => {
    if (!file) return;
    setPendingFile(file);
    setFormError('');
    // 用户没写项目名时，用文件名兜个默认值，减少一步手动输入
    if (!projectName.trim()) {
      setProjectName(file.name.replace(/\.[^.]+$/, ''));
    }
  };

  const handleCreate = async () => {
    setFormError('');
    const name = projectName.trim();
    if (!name) {
      setFormError(t('project.nameRequired'));
      return;
    }

    setSubmitting(true);
    try {
      // 风格：自定义模式必须填写，否则回落到预设。
      // ⚠️ 预设位留空（目录未到）时回落到默认风格，不能让「新建项目」被取数时序卡住；
      //    自定义模式留空仍按原逻辑报错（静默换默认值会让用户以为填了自定义风格）。
      let finalStyle = stylePreset === CUSTOM_STYLE
        ? customStyle.trim()
        : (stylePreset || defaultStyleValue);
      if (!finalStyle) {
        setFormError(t('project.styleRequired'));
        setSubmitting(false);
        return;
      }

      let novelId = '';
      let parsedChapters: number | null = null;

      if (source === 'upload') {
        if (!pendingFile) {
          setFormError(t('project.needNovelFile'));
          return;
        }
        // autoProject=false：不让后端按小说标题自动建项目，
        // 而是由前端带着用户填的名称显式创建，避免名字对不上。
        const up = await novelsApi.upload(pendingFile, { autoProject: false });
        const first = (up.results || [])[0];
        if (!first?.success || !first.novel?.novel_id) {
          throw new Error(first?.error || t('upload.failed'));
        }
        novelId = first.novel.novel_id;
        parsedChapters = first.novel.chapter_count ?? null;
      } else {
        novelId = selectedNovel;
        if (!novelId) {
          setFormError(t('project.selectNovelPlaceholder'));
          return;
        }
      }

      const res = await projectsApi.create({
        name,
        novel_id: novelId,
        config: {
          ...DEFAULT_CONFIG,
          style: finalStyle,
          aspect_ratio: aspectRatio,
          // 视频生成方式：整集一次生成 / 逐镜生成 / 首尾帧驱动（后端会归一校验）
          video_mode: videoMode,
        },
      } as any);
      const key = res?.project?.dir_key || res?.project?.id || '';

      await reload();
      setShowNewProject(false);
      resetForm();

      if (key) {
        // 走 router 而不是写 `window.location.hash` —— 后者写出的 `/?p=<key>`
        // 还得靠 `LegacyRouteRedirect` 在 hash 层 301 一次才落到工作台。
        navigate(workbenchPath(key));
      }
      // 上传路径成功后反馈解析出的章节数（"选择已有小说"路径无此信息）。
      if (parsedChapters !== null) {
        toast.success(t('project.chapterParsed', { n: parsedChapters }));
      }
    } catch (e) {
      setFormError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const busyLabel = useMemo(() => {
    if (!submitting) return t('project.create');
    return source === 'upload' ? t('project.uploadingCreating') : t('project.creating');
  }, [submitting, source, t]);

  // --- 编辑项目 ---
  const openEditModal = (proj: Project) => {
    setEditingProject(proj);
    setEditName(proj.name);
    setEditError('');
  };

  const closeEditModal = () => {
    setEditingProject(null);
    setEditName('');
    setEditError('');
    setSavingEdit(false);
  };

  const handleSaveEdit = async () => {
    if (!editingProject) return;
    const newName = editName.trim();
    if (!newName) {
      setEditError(t('project.nameRequired'));
      return;
    }
    setSavingEdit(true);
    setEditError('');
    try {
      await projectsApi.rename(editingProject.dir_key || editingProject.id, newName);
      await reload();
      closeEditModal();
    } catch (e) {
      setEditError(e instanceof Error ? e.message : t('project.saveFailed'));
    } finally {
      setSavingEdit(false);
    }
  };

  // --- 删除项目 ---
  const openDeleteModal = (proj: Project) => {
    setDeletingProject(proj);
    setDeleteError('');
  };

  const closeDeleteModal = () => {
    setDeletingProject(null);
    setDeleteError('');
  };

  const handleDelete = async () => {
    if (!deletingProject) return;
    setDeleting(true);
    setDeleteError('');
    try {
      await projectsApi.deleteV2(deletingProject.dir_key || deletingProject.id, true);
      await reload();
      closeDeleteModal();
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : t('project.deleteFailed'));
    } finally {
      setDeleting(false);
    }
  };

  // --- 项目封面 ---
  const handleGenCover = async (proj: Project) => {
    const key = proj.dir_key || proj.id;
    setCoverBusy(proj.id);
    try {
      const res = await projectsApi.generateCover(key);
      // 带时间戳击穿浏览器缓存，旧封面立即被替换
      setCoverUrls(prev => ({ ...prev, [proj.id]: res?.cover_url || `${projectsApi.coverUrl(key)}?t=${Date.now()}` }));
      await reload();
      toast.success(t('project.coverDone'));
    } catch (e) {
      toast.error(`${t('project.coverFailed')}: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setCoverBusy('');
    }
  };

  // 加载态：沿用真实内容的外层与卡片网格列数，避免骨架 → 内容的布局跳变
  if (loading) {
    return (
      <div
        className="space-y-6 fade-in"
        role="status"
        aria-live="polite"
        aria-label={t('common.loading')}
      >
        <div className="flex items-center justify-between">
          <div className="space-y-2">
            <Skeleton className="h-7 w-32" />
            <Skeleton className="h-4 w-56" />
          </div>
          <Skeleton className="h-9 w-28" />
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {Array.from({ length: 6 }).map((_, i) => (
            <Skeleton key={i} className="h-32 rounded-xl" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6 fade-in">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-ink-1">{t('project.title')}</h2>
          <p className="text-sm text-ink-2 mt-1">{t('project.chooseOrUpload')}</p>
        </div>
        {/* whitespace-nowrap + shrink-0：375 视口下按钮文字会被挤成两行 */}
        <Button onClick={openModal} className="shrink-0 whitespace-nowrap">
          <Plus className="h-4 w-4" />
          {t('project.createNew')}
        </Button>
      </div>

      {/* 软失败：项目列表非空 → 只是这一次刷新失败，保留紧凑行内提示条，绝不吃掉已展示的列表 */}
      {loadError && projects.length > 0 && (
        <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm">
          {t('project.loadingFailed')}: {loadError}
        </div>
      )}

      {projects.length === 0 ? (
        loadError ? (
          /* 硬失败：项目列表为空且加载出错，没有任何数据可展示 → 整块错误态 + 重试 */
          <ErrorState
            title={t('project.loadingFailed')}
            description={loadError}
            onRetry={() => {
              setLoadError('');
              setLoading(true);
              reload()
                .catch((e) => setLoadError(e instanceof Error ? e.message : String(e)))
                .finally(() => setLoading(false));
            }}
          />
        ) : (
          <EmptyState
            icon={<FolderOpen className="h-10 w-10" />}
            title={t('project.noProjects')}
            description={t('project.noProjectsHint')}
            action={<Button onClick={openModal}>{t('project.createNew')}</Button>}
          />
        )
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {projects.map((proj) => {
            // 封面地址与「有无封面」同时被封面图和「生成封面」按钮使用，提到 map 作用域
            const coverKey = proj.dir_key || proj.id;
            const coverSrc = coverUrls[proj.id]
              || (proj.has_cover ? projectsApi.coverUrl(coverKey) : '');
            const hasCover = Boolean(coverSrc);
            return (
            <div
              key={proj.id}
              className="group relative bg-surface rounded-xl border border-line p-4 hover:shadow-lg transition-shadow"
            >
              {/* 整卡 = 真正的 <Link>。
                  改造前是 `role="button" tabIndex={0}` 的 div + 手写 Enter/Space：
                  ① 模拟语义 —— 读屏把它念成「按钮」，可它做的是导航；
                  ② 手写键盘处理是原生元素行为的重复品（换原生元素后必须删，否则双触发）；
                  ③ 跳转写的是 `window.location.hash = '/?p=…'`，绕过 router，
                     要等 LegacyRouteRedirect 在 hash 层 301 一次才落到工作台。
                  「生成封面」按钮改为与 <Link> **平级**：<a> 内不允许嵌套交互元素，
                  平级之后它点自己时不会触发卡片跳转，原来的 stopPropagation 也就不需要了。 */}
              <Link
                to={workbenchPath(coverKey)}
                className="block rounded-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2"
              >
                <div className="relative aspect-video bg-surface-2 rounded-lg mb-4 flex items-center justify-center overflow-hidden group-hover:scale-105 transition-transform">
                  {hasCover
                    ? <img src={coverSrc} alt={proj.name} className="h-full w-full object-cover" />
                    : <Clapperboard className="h-10 w-10 text-ink-3" />}
                </div>
                <h3 className="font-semibold text-ink-1 mb-1">{proj.name}</h3>
                <p className="text-sm text-ink-2 mb-3">
                  {t('project.style')}: {proj.config?.style || '—'}
                </p>
                <div className="flex items-center justify-between text-sm">
                  <Badge variant="info">{proj.episode_count} {t('ep.suffix')}</Badge>
                  <span className="text-ink-3">{new Date(proj.created_at).toLocaleDateString()}</span>
                </div>
              </Link>

              {/* 生成/换封面：绝对定位回封面图右上角（卡片 p-4 + 图内 right-2 = right-6）。
                  与 <Link> 平级 → 点击它只生成封面，不会跳工作台。 */}
              <button
                type="button"
                disabled={coverBusy === proj.id}
                onClick={() => handleGenCover(proj)}
                className="absolute right-6 top-6 inline-flex items-center gap-1 rounded-md bg-black/45 px-2 py-1 text-xs font-medium text-white hover:bg-black/65 disabled:opacity-60 focus:outline-none focus-visible:ring-2 focus-visible:ring-white/70"
              >
                <ImageIcon className="h-3.5 w-3.5" />
                {coverBusy === proj.id
                  ? t('project.coverGenerating')
                  : hasCover ? t('project.coverRedo') : t('project.coverGen')}
              </button>

              {/* 操作按钮 */}
              <div className="flex gap-2 mt-3 pt-3 border-t border-line">
                {/* 编辑/删除按钮与可点击卡片是兄弟节点（不嵌套），无需再 stopPropagation */}
                <Button
                  variant="secondary"
                  size="sm"
                  className="flex-1"
                  onClick={() => {
                    setEditingProject(proj);
                    setEditName(proj.name);
                    setEditError('');
                  }}
                >
                  <Pencil className="h-4 w-4" /> {t('project.edit')}
                </Button>
                <Button
                  variant="danger"
                  size="sm"
                  onClick={() => openDeleteModal(proj)}
                >
                  <Trash2 className="h-4 w-4" /> {t('project.delete')}
                </Button>
              </div>
            </div>
            );
          })}
        </div>
      )}

      {/* 新建项目（上传小说 / 选已有小说 二合一） */}
      <Modal isOpen={showNewProject} onClose={closeModal} title={t('project.createNew')}
        size="lg" closeOnBackdrop={false} closeOnEsc={false} preventClose={submitting}>
        <div className="space-y-4">
          {/* 项目名称 */}
          <div>
            <Input
              value={projectName}
              onChange={setProjectName}
              label={t('project.name')}
              placeholder={t('project.namePlaceholder')}
            />
          </div>

          {/* 风格库：分类标签 + 缩略图卡片 + 自定义（对标 pavo 风格库交互）。
              数据取自 GET /api/styles；组件内部保持原交互与文案。 */}
          <div>
            <label className="mb-1 block text-sm font-medium text-ink-2">
              {t('project.style')}
            </label>
            <StyleGallery
              styles={catalogStyles}
              counts={styleCounts}
              cat={styleCat}
              onCatChange={setStyleCat}
              selected={stylePreset}
              onSelect={(v) => { setStylePreset(v); setFormError(''); }}
              customStyle={customStyle}
              onCustomChange={(v) => { setCustomStyle(v); setFormError(''); }}
              loading={stylesLoading}
              error={stylesError}
            />
          </div>

          {/* 画面比例：与风格一起在新建入口统一设置，决定视频/分镜画幅 */}
          <div>
            <Select
              value={aspectRatio}
              onChange={(v) => { setAspectRatio(v); setFormError(''); }}
              label={t('project.aspectRatio')}
              options={aspectPresets.map(p => ({
                value: p.value,
                label: t(ASPECT_LABEL_KEYS[p.aspect_id] || p.aspect_id),
              }))}
            />
          </div>

          {/* 视频生成方式：新建时就定下来（整集一次生成 / 逐镜生成 / 首尾帧驱动），
              之后「生成视频」与托管生产都按它执行 —— 避免「功能有、入口没有」。 */}
          <div>
            <label className="block text-sm font-medium text-ink-1 mb-1">
              {t('project.videoMode')}
            </label>
            <div className="grid grid-cols-3 gap-2">
              {VIDEO_MODE_OPTIONS.map((opt) => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => { setVideoMode(opt.value); setFormError(''); }}
                  className={`rounded-lg border px-3 py-2 text-left transition-colors ${
                    videoMode === opt.value
                      ? 'border-brand bg-brand/10'
                      : 'border-line-strong hover:bg-surface-2'
                  }`}
                >
                  <span className={`block text-sm font-medium ${videoMode === opt.value ? 'text-brand' : 'text-ink-2'}`}>
                    {t(opt.labelKey)}
                  </span>
                  <span className="mt-0.5 block text-[11px] leading-tight text-ink-3">
                    {t(opt.descKey)}
                  </span>
                </button>
              ))}
            </div>
          </div>

          {/* 小说来源切换 */}
          <div>
            <label className="block text-sm font-medium text-ink-1 mb-1">
              {t('project.novelSource')}
            </label>
            <div className="inline-flex rounded-lg border border-line-strong overflow-hidden">
              {/* 保留原生：分段开关（选中态共用同一元素），
                  Button 的 rounded-md/h-8 会破坏「无间隙拼成一个圆角容器」的形状 */}
              {([
                { id: 'upload' as NovelSource, label: t('project.sourceUpload') },
                { id: 'existing' as NovelSource, label: t('project.sourceExisting') },
              ]).map((opt) => (
                <button
                  key={opt.id}
                  type="button"
                  onClick={() => { setSource(opt.id); setFormError(''); }}
                  className={`px-4 py-2 text-sm font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 focus-visible:ring-offset-2 focus-visible:ring-offset-canvas ${
                    source === opt.id
                      ? 'bg-brand text-white'
                      : 'bg-transparent text-ink-2 hover:bg-surface-2'
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          </div>

          {/* 上传模式 */}
          {source === 'upload' && (
            <div>
              <div
                onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
                onDragLeave={() => setDragOver(false)}
                onDrop={(e) => { e.preventDefault(); setDragOver(false); pickFile(e.dataTransfer.files?.[0]); }}
                onClick={() => fileInputRef.current?.click()}
                className={`border-2 border-dashed rounded-xl p-6 text-center cursor-pointer transition-colors ${
                  dragOver
                    ? 'border-brand bg-info-subtle'
                    : 'border-line-strong hover:border-brand'
                }`}
              >
                <div className="mb-2 flex justify-center text-ink-3">
                  <FileText className="h-8 w-8" />
                </div>
                <p className="text-sm font-medium text-ink-1">
                  {pendingFile ? pendingFile.name : t('upload.uploadText')}
                </p>
                <p className="text-xs text-ink-2 mt-1">
                  {pendingFile
                    ? `${(pendingFile.size / 1024).toFixed(0)} KB`
                    : t('upload.fileHint')}
                </p>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept={ACCEPT_EXTS}
                  className="hidden"
                  onChange={(e) => pickFile(e.target.files?.[0])}
                />
              </div>
            </div>
          )}

          {/* 选择已有模式 */}
          {source === 'existing' && (
            <div>
              {novels.length === 0 ? (
                <p className="text-sm text-ink-2 py-3">
                  {t('project.noNovelsYet')}
                </p>
              ) : (
                <Select
                  value={selectedNovel}
                  onChange={(v) => { setSelectedNovel(v); setFormError(''); }}
                  options={[
                    { value: '', label: t('project.selectNovelPlaceholder') },
                    ...novels.map((n) => ({
                      value: n.novel_id,
                      label: t('project.novelOption', { name: n.name, n: n.chapter_count }),
                    })),
                  ]}
                />
              )}
            </div>
          )}

          {formError && (
            <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm break-words">
              {formError}
            </div>
          )}

          <div className="flex gap-3 pt-2">
            <Button variant="secondary" onClick={closeModal} disabled={submitting}>
              {t('common.cancel')}
            </Button>
            <Button onClick={handleCreate} disabled={submitting}>
              {busyLabel}
            </Button>
          </div>
        </div>
      </Modal>

      {/* 编辑项目弹窗 */}
      {editingProject && (
        <Modal isOpen={!!editingProject} onClose={closeEditModal} title={t('project.editTitle')}
          closeOnBackdrop={false} closeOnEsc={false} preventClose={savingEdit}>
          <div className="space-y-4">
            <div>
              <Input value={editName} onChange={setEditName} label={t('project.name')} />
            </div>
            {editError && (
              <div className="p-3 bg-danger-subtle border border-danger/30 rounded-lg text-danger-strong text-sm">
                {editError}
              </div>
            )}
            <div className="flex gap-3 pt-2">
              <Button variant="secondary" onClick={closeEditModal} disabled={savingEdit}>
                {t('common.cancel')}
              </Button>
              <Button onClick={handleSaveEdit} disabled={savingEdit}>
                {savingEdit ? t('common.saving') : t('common.save')}
              </Button>
            </div>
          </div>
        </Modal>
      )}

      {/* 删除项目：统一确认弹窗（原生手写两步确认已被 ConfirmDialog 取代） */}
      <ConfirmDialog
        isOpen={!!deletingProject}
        onClose={closeDeleteModal}
        onConfirm={handleDelete}
        title={t('project.deleteTitle')}
        danger
        loading={deleting}
        confirmText={t('project.confirmDelete')}
        message={
          <>
            <p className="text-ink-1">
              {t('project.deleteConfirmPrefix')}<span className="font-semibold">{deletingProject?.name}</span>{t('project.deleteConfirmSuffix')}
            </p>
            <p className="mt-2 flex items-start gap-1.5 text-sm text-danger-strong">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              {t('project.deleteWarning')}
            </p>
            {deleteError && (
              <p className="mt-3 rounded-lg border border-danger/30 bg-danger-subtle p-3 text-sm text-danger-strong">
                {deleteError}
              </p>
            )}
          </>
        }
      />
    </div>
  );
}
