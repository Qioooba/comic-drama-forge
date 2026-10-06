// ============================================================
// 本文件是**自动生成物**，禁止手改。
//
// 生成命令：python scripts/generate_client.py
// 契约来源：app/contracts/openapi.py → GET /api/contracts/openapi.json
// 校验门禁：python scripts/check_generated_client.py
//
// 手改这里没有任何意义：下一次生成会被逐字覆盖，且 CI 会因
// 「生成物已过期」而失败。要改接口，去改后端 + 规格，然后重新生成。
// ============================================================

import type {
  ApprovalView,
  ApproveDeliveryPackageRequest,
  ApproveMediaRequest,
  Attempt,
  AttemptResponse,
  BrandKit,
  BrandKitsResponse,
  CapabilityProfileListResponse,
  CapabilityProfileRequest,
  CapabilityProfileResponse,
  CapabilityProfileVersion,
  ContractVersion,
  ContractVersionResponse,
  CreateAttemptRequest,
  CreateDeliveryPackageRequest,
  CreateIntentRequest,
  CreateJobRequest,
  CreateTimelineRevisionRequest,
  DecisionListResponse,
  DecisionRecord,
  DecisionResponse,
  DeliveryFile,
  DeliveryManifest,
  DeliveryManifestResponse,
  DeliveryPackage,
  DeliveryPackageListResponse,
  DeliveryPackageResponse,
  DeliveryPreset,
  DeliveryPresetsResponse,
  EpisodeRenderVersion,
  ErrorCodesResponse,
  ErrorResponse,
  FinishAttemptRequest,
  GateViolation,
  GenerationIntent,
  IntentListResponse,
  IntentResponse,
  Job,
  JobListResponse,
  JobResponse,
  JobStats,
  JobStatsResponse,
  LicenseEntry,
  LicenseRegistryResponse,
  LicenseRegistrySummary,
  LicenseRequirement,
  LicensedMusicResponse,
  LicensingGateResponse,
  LicensingGateResult,
  MediaVersion,
  MediaVersionListResponse,
  MediaVersionResponse,
  OkResponse,
  OpenApiSpecResponse,
  PendingUnit,
  PendingUnitListResponse,
  PreflightReport,
  PreflightResponse,
  RegisterMediaRequest,
  ReleaseCheckRequest,
  ReleaseCheckResponse,
  RenderManifest,
  RenderManifestResponse,
  RenderRequest,
  RenderVersionListResponse,
  RenderVersionResponse,
  ReuseHintResponse,
  RevokeApprovalRequest,
  RevokeDeliveryApprovalRequest,
  SelectMediaRequest,
  StyleEntry,
  StyleListResponse,
  StyleResolveResponse,
  StyleResponse,
  TimelineItem,
  TimelineRevision,
  TimelineRevisionListResponse,
  TimelineRevisionResponse,
  UpdateAttemptRequest,
  UpdateJobRequest,
  VerifyDeliveryResponse,
  VerifyRenderRequest,
  VerifyRenderResponse,
  VerifyResult,
} from './types';

const API_BASE = '/api';


// 后端统一返回 {success, error?, message?}。此前 request() 直接抛
// `HTTP 500: Internal Server Error`，把后端精心脱敏过的中文错误丢掉了，
// 界面上只能看到无信息的英文报错。这里优先取后端的可读文案。
async function readError(response: Response): Promise<string> {
  let detail = '';
  try {
    const data = await response.clone().json();
    const raw = data?.error || data?.message || data?.detail;
    if (typeof raw === 'string' && raw.trim()) detail = raw.trim();
    else if (raw) detail = JSON.stringify(raw);
    // 后端常带 `hint`（例如「这集可能是兜底生成的，没有台词」）或 `guide`
    // （例如视觉模型不适配的替代建议）。这些是给用户看的处置办法，
    // 只把 error 抛出去会让用户看到问题却不知道怎么办。
    const extra = data?.hint || data?.guide || data?.layout_hint;
    if (typeof extra === 'string' && extra.trim()) {
      detail = detail ? `${detail}（${extra.trim()}）` : extra.trim();
    }
  } catch {
    try {
      const text = (await response.text()).trim();
      if (text) detail = text;
    } catch {
      /* 响应体不可读，退化为状态码 */
    }
  }
  const status = `HTTP ${response.status}`;
  return detail ? `${detail}` : `${status} ${response.statusText || ''}`.trim();
}


async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  // FormData 必须让浏览器自行生成 multipart boundary —— 一旦手工带上
  // Content-Type: application/json，boundary 就没了，后端 request.files 收到空列表。
  const isForm = typeof FormData !== 'undefined' && options.body instanceof FormData;
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: isForm
      ? { ...options.headers }
      : { 'Content-Type': 'application/json', ...options.headers },
  });
  if (!response.ok) {
    throw new Error(await readError(response));
  }
  return response.json() as Promise<T>;
}

/** 查询串拼装：跳过 undefined / null / 空串，避免出现 `?project=` 这种脏 URL */
function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join('&')}` : '';
}


/**
 * contracts —— 契约版本 2026-10-07.2（spec 4c0774a9aeeb…）
 */
export const contractsApi = {
  /**
   * 交付与授权的错误码对照表
   *
   * `GET /api/contracts/error-codes`
   */
  getErrorCodes: () =>
    request<ErrorCodesResponse>(`/api/contracts/error-codes`, { method: 'GET' }),
  /**
   * 导出 OpenAPI 3.1 规格（契约本体）
   *
   * `GET /api/contracts/openapi.json`
   */
  getOpenapiSpec: () =>
    request<OpenApiSpecResponse>(`/api/contracts/openapi.json`, { method: 'GET' }),
  /**
   * 契约版本与规格摘要（前端启动闸门用）
   *
   * `GET /api/contracts/version`
   */
  getContractVersion: () =>
    request<ContractVersionResponse>(`/api/contracts/version`, { method: 'GET' }),
};

/**
 * delivery —— 契约版本 2026-10-07.2（spec 4c0774a9aeeb…）
 */
export const deliveryApi = {
  /**
   * 交付包列表（含机器校验与人工批准状态）
   *
   * `GET /api/delivery/packages`
   */
  listDeliveryPackages: (project?: string) =>
    request<DeliveryPackageListResponse>(`/api/delivery/packages${qs({ project })}`, { method: 'GET' }),
  /**
   * 登记交付包（扫描既有导出产物并算 SHA-256）
   *
   * `POST /api/delivery/packages`
   */
  createDeliveryPackage: (body: CreateDeliveryPackageRequest) =>
    request<DeliveryPackageResponse>(`/api/delivery/packages`, { method: 'POST', body: JSON.stringify(body) }),
  /**
   * 交付包详情（含文件清单与 SHA-256）
   *
   * `GET /api/delivery/packages/{package_id}`
   */
  getDeliveryPackage: (package_id: string) =>
    request<DeliveryPackageResponse>(`/api/delivery/packages/${encodeURIComponent(String(package_id))}`, { method: 'GET' }),
  /**
   * 人工批准（须带操作者，批准与包哈希绑定）
   *
   * `POST /api/delivery/packages/{package_id}/approve`
   */
  approveDeliveryPackage: (package_id: string, body: ApproveDeliveryPackageRequest) =>
    request<DeliveryPackageResponse>(`/api/delivery/packages/${encodeURIComponent(String(package_id))}/approve`, { method: 'POST', body: JSON.stringify(body) }),
  /**
   * 交付清单（逐文件 SHA-256 + 包摘要）
   *
   * `GET /api/delivery/packages/{package_id}/manifest`
   */
  getDeliveryManifest: (package_id: string) =>
    request<DeliveryManifestResponse>(`/api/delivery/packages/${encodeURIComponent(String(package_id))}/manifest`, { method: 'GET' }),
  /**
   * 交付发布总门禁（授权+校验+人工批准+磁盘）
   *
   * `POST /api/delivery/packages/{package_id}/release-check`
   */
  releaseCheckDeliveryPackage: (package_id: string, body?: ReleaseCheckRequest) =>
    request<ReleaseCheckResponse>(`/api/delivery/packages/${encodeURIComponent(String(package_id))}/release-check`, { method: 'POST', body: JSON.stringify(body) }),
  /**
   * 撤销人工批准
   *
   * `POST /api/delivery/packages/{package_id}/revoke`
   */
  revokeDeliveryApproval: (package_id: string, body: RevokeDeliveryApprovalRequest) =>
    request<DeliveryPackageResponse>(`/api/delivery/packages/${encodeURIComponent(String(package_id))}/revoke`, { method: 'POST', body: JSON.stringify(body) }),
  /**
   * 机器校验：逐文件重算 SHA-256
   *
   * `POST /api/delivery/packages/{package_id}/verify`
   */
  verifyDeliveryPackage: (package_id: string) =>
    request<VerifyDeliveryResponse>(`/api/delivery/packages/${encodeURIComponent(String(package_id))}/verify`, { method: 'POST' }),
  /**
   * 交付预设列表（竖屏 / 横屏 / 3:4）
   *
   * `GET /api/delivery/presets`
   */
  listDeliveryPresets: () =>
    request<DeliveryPresetsResponse>(`/api/delivery/presets`, { method: 'GET' }),
};

/**
 * jobs —— 契约版本 2026-10-07.2（spec 4c0774a9aeeb…）
 */
export const jobsApi = {
  /**
   * 作业列表（Job = 一次用户意图）
   *
   * `GET /api/jobs`
   */
  listJobs: (status?: string) =>
    request<JobListResponse>(`/api/jobs${qs({ status })}`, { method: 'GET' }),
  /**
   * 创建作业
   *
   * `POST /api/jobs`
   */
  createJob: (body: CreateJobRequest) =>
    request<JobResponse>(`/api/jobs`, { method: 'POST', body: JSON.stringify(body) }),
  /**
   * 结束尝试并结算
   *
   * `POST /api/jobs/attempts/{attempt_id}/finish`
   */
  finishJobAttempt: (attempt_id: string, body: FinishAttemptRequest) =>
    request<AttemptResponse>(`/api/jobs/attempts/${encodeURIComponent(String(attempt_id))}/finish`, { method: 'POST', body: JSON.stringify(body) }),
  /**
   * 更新尝试状态
   *
   * `POST /api/jobs/attempts/{attempt_id}/status`
   */
  updateAttemptStatus: (attempt_id: string, body: UpdateAttemptRequest) =>
    request<AttemptResponse>(`/api/jobs/attempts/${encodeURIComponent(String(attempt_id))}/status`, { method: 'POST', body: JSON.stringify(body) }),
  /**
   * 作业统计（按状态聚合）
   *
   * `GET /api/jobs/stats`
   */
  getJobStats: (project?: string) =>
    request<JobStatsResponse>(`/api/jobs/stats${qs({ project })}`, { method: 'GET' }),
  /**
   * 作业详情（含各次尝试）
   *
   * `GET /api/jobs/{job_id}`
   */
  getJob: (job_id: string) =>
    request<JobResponse>(`/api/jobs/${encodeURIComponent(String(job_id))}`, { method: 'GET' }),
  /**
   * 为作业开启一次尝试（Attempt = 一次执行）
   *
   * `POST /api/jobs/{job_id}/attempts`
   */
  createJobAttempt: (job_id: string, body?: CreateAttemptRequest) =>
    request<AttemptResponse>(`/api/jobs/${encodeURIComponent(String(job_id))}/attempts`, { method: 'POST', body: JSON.stringify(body) }),
  /**
   * 待处理单元（断点续跑判据之一）
   *
   * `GET /api/jobs/{job_id}/pending-units`
   */
  listPendingUnits: (job_id: string) =>
    request<PendingUnitListResponse>(`/api/jobs/${encodeURIComponent(String(job_id))}/pending-units`, { method: 'GET' }),
  /**
   * 免重渲提示（workflow_hash 命中可复用）
   *
   * `GET /api/jobs/{job_id}/reuse-hint`
   */
  getJobReuseHint: (job_id: string) =>
    request<ReuseHintResponse>(`/api/jobs/${encodeURIComponent(String(job_id))}/reuse-hint`, { method: 'GET' }),
  /**
   * 更新作业状态
   *
   * `POST /api/jobs/{job_id}/status`
   */
  updateJobStatus: (job_id: string, body: UpdateJobRequest) =>
    request<JobResponse>(`/api/jobs/${encodeURIComponent(String(job_id))}/status`, { method: 'POST', body: JSON.stringify(body) }),
};

/**
 * licensing —— 契约版本 2026-10-07.2（spec 4c0774a9aeeb…）
 */
export const licensingApi = {
  /**
   * 品牌与水印版本列表
   *
   * `GET /api/licensing/brands`
   */
  listBrandKits: () =>
    request<BrandKitsResponse>(`/api/licensing/brands`, { method: 'GET' }),
  /**
   * 授权门禁预检（交付前自查）
   *
   * `POST /api/licensing/gate`
   */
  evaluateLicensingGate: (body?: LicenseRequirement) =>
    request<LicensingGateResponse>(`/api/licensing/gate`, { method: 'POST', body: JSON.stringify(body) }),
  /**
   * 音乐库授权列表（CC BY 署名可直接复制）
   *
   * `GET /api/licensing/music`
   */
  listLicensedMusic: () =>
    request<LicensedMusicResponse>(`/api/licensing/music`, { method: 'GET' }),
  /**
   * 授权登记表全量（模型 / 素材 / 品牌）
   *
   * `GET /api/licensing/registry`
   */
  getLicenseRegistry: () =>
    request<LicenseRegistryResponse>(`/api/licensing/registry`, { method: 'GET' }),
};

/**
 * production_facts —— 契约版本 2026-10-07.2（spec 4c0774a9aeeb…）
 */
export const productionfactsApi = {
  /**
   * 撤销人工批准（须人工授权者）
   *
   * `POST /api/production_facts/approvals/{approval_id}/revoke`
   */
  revokeProductionApproval: (approval_id: string, body: RevokeApprovalRequest) =>
    request<DecisionResponse>(`/api/production_facts/approvals/${encodeURIComponent(String(approval_id))}/revoke`, { method: 'POST', body: JSON.stringify(body) }),
  /**
   * 能力档案版本列表
   *
   * `GET /api/production_facts/capability-profiles`
   */
  listCapabilityProfiles: () =>
    request<CapabilityProfileListResponse>(`/api/production_facts/capability-profiles`, { method: 'GET' }),
  /**
   * 登记能力档案版本
   *
   * `POST /api/production_facts/capability-profiles`
   */
  registerCapabilityProfile: (body: CapabilityProfileRequest) =>
    request<CapabilityProfileResponse>(`/api/production_facts/capability-profiles`, { method: 'POST', body: JSON.stringify(body) }),
  /**
   * 采用/批准决策列表（decision_state 五态）
   *
   * `GET /api/production_facts/decisions`
   */
  listDecisions: (media_version_id?: string) =>
    request<DecisionListResponse>(`/api/production_facts/decisions${qs({ media_version_id })}`, { method: 'GET' }),
  /**
   * 决策变更历史（追加式，不覆盖）
   *
   * `GET /api/production_facts/decisions/history`
   */
  listDecisionHistory: (media_version_id?: string) =>
    request<DecisionListResponse>(`/api/production_facts/decisions/history${qs({ media_version_id })}`, { method: 'GET' }),
  /**
   * 生成意图列表（冻结后的生成事实）
   *
   * `GET /api/production_facts/intents`
   */
  listProductionIntents: (project?: string, intent_id?: string) =>
    request<IntentListResponse>(`/api/production_facts/intents${qs({ project, intent_id })}`, { method: 'GET' }),
  /**
   * 登记生成意图（冻结：此后不可改）
   *
   * `POST /api/production_facts/intents`
   */
  createProductionIntent: (body: CreateIntentRequest) =>
    request<IntentResponse>(`/api/production_facts/intents`, { method: 'POST', body: JSON.stringify(body) }),
  /**
   * 生成意图详情
   *
   * `GET /api/production_facts/intents/{intent_id}`
   */
  getProductionIntent: (intent_id: string) =>
    request<IntentResponse>(`/api/production_facts/intents/${encodeURIComponent(String(intent_id))}`, { method: 'GET' }),
  /**
   * 派生新意图（冻结意图不可原地改）
   *
   * `POST /api/production_facts/intents/{intent_id}/derive`
   */
  deriveProductionIntent: (intent_id: string) =>
    request<IntentResponse>(`/api/production_facts/intents/${encodeURIComponent(String(intent_id))}/derive`, { method: 'POST' }),
  /**
   * 候选媒体版本列表（含采用/批准状态）
   *
   * `GET /api/production_facts/media`
   */
  listMediaVersions: (intent_id?: string, include_state?: string) =>
    request<MediaVersionListResponse>(`/api/production_facts/media${qs({ intent_id, include_state })}`, { method: 'GET' }),
  /**
   * 登记一次生成产出的候选
   *
   * `POST /api/production_facts/media`
   */
  registerMediaVersion: (body: RegisterMediaRequest) =>
    request<MediaVersionResponse>(`/api/production_facts/media`, { method: 'POST', body: JSON.stringify(body) }),
  /**
   * 媒体版本详情
   *
   * `GET /api/production_facts/media/{media_version_id}`
   */
  getMediaVersion: (media_version_id: string) =>
    request<MediaVersionResponse>(`/api/production_facts/media/${encodeURIComponent(String(media_version_id))}`, { method: 'GET' }),
  /**
   * 批准放行（人工决定；须先采用）
   *
   * `POST /api/production_facts/media/{media_version_id}/approve`
   */
  approveMediaVersion: (media_version_id: string, body: ApproveMediaRequest) =>
    request<DecisionResponse>(`/api/production_facts/media/${encodeURIComponent(String(media_version_id))}/approve`, { method: 'POST', body: JSON.stringify(body) }),
  /**
   * 采用这版（创作决定；永不隐式升级为批准）
   *
   * `POST /api/production_facts/media/{media_version_id}/select`
   */
  selectMediaVersion: (media_version_id: string, body: SelectMediaRequest) =>
    request<DecisionResponse>(`/api/production_facts/media/${encodeURIComponent(String(media_version_id))}/select`, { method: 'POST', body: JSON.stringify(body) }),
};

/**
 * styles —— 契约版本 2026-10-07.2（spec 4c0774a9aeeb…）
 */
export const stylesApi = {
  /**
   * 风格库全量（后端单一事实源）
   *
   * `GET /api/styles`
   */
  listStyles: (category?: string) =>
    request<StyleListResponse>(`/api/styles${qs({ category })}`, { method: 'GET' }),
  /**
   * 由自由文本解析风格（存量项目向后兼容）
   *
   * `GET /api/styles/resolve`
   */
  resolveStyle: (text: string) =>
    request<StyleResolveResponse>(`/api/styles/resolve${qs({ text })}`, { method: 'GET' }),
  /**
   * 风格详情（stable style_id，改名不断历史）
   *
   * `GET /api/styles/{style_id}`
   */
  getStyle: (style_id: string) =>
    request<StyleResponse>(`/api/styles/${encodeURIComponent(String(style_id))}`, { method: 'GET' }),
};

/**
 * timeline —— 契约版本 2026-10-07.2（spec 4c0774a9aeeb…）
 */
export const timelineApi = {
  /**
   * 按合成指纹反查 revision
   *
   * `GET /api/timeline/fingerprints/{fingerprint}`
   */
  getTimelineByFingerprint: (fingerprint: string) =>
    request<TimelineRevisionResponse>(`/api/timeline/fingerprints/${encodeURIComponent(String(fingerprint))}`, { method: 'GET' }),
  /**
   * 按剪辑指纹反查成片登记
   *
   * `GET /api/timeline/renders/by-fingerprint/{compose_fingerprint}`
   */
  findTimelineRenderVersions: (compose_fingerprint: string) =>
    request<RenderVersionListResponse>(`/api/timeline/renders/by-fingerprint/${encodeURIComponent(String(compose_fingerprint))}`, { method: 'GET' }),
  /**
   * 读取成片登记（含指纹与 sha256）
   *
   * `GET /api/timeline/renders/{render_id}`
   */
  getTimelineRenderVersion: (render_id: string) =>
    request<RenderVersionResponse>(`/api/timeline/renders/${encodeURIComponent(String(render_id))}`, { method: 'GET' }),
  /**
   * 时间线 revision 列表（不可变）
   *
   * `GET /api/timeline/revisions`
   */
  listTimelineRevisions: (project?: string) =>
    request<TimelineRevisionListResponse>(`/api/timeline/revisions${qs({ project })}`, { method: 'GET' }),
  /**
   * 冻结一个时间线 revision
   *
   * `POST /api/timeline/revisions`
   */
  createTimelineRevision: (body: CreateTimelineRevisionRequest) =>
    request<TimelineRevisionResponse>(`/api/timeline/revisions`, { method: 'POST', body: JSON.stringify(body) }),
  /**
   * 该集的最新时间线 revision
   *
   * `GET /api/timeline/revisions/latest`
   */
  getLatestTimelineRevision: (project?: string, episode?: string) =>
    request<TimelineRevisionResponse>(`/api/timeline/revisions/latest${qs({ project, episode })}`, { method: 'GET' }),
  /**
   * 时间线 revision 详情
   *
   * `GET /api/timeline/revisions/{revision_id}`
   */
  getTimelineRevision: (revision_id: string) =>
    request<TimelineRevisionResponse>(`/api/timeline/revisions/${encodeURIComponent(String(revision_id))}`, { method: 'GET' }),
  /**
   * 由既有 revision 派生新版本（不可原地改）
   *
   * `POST /api/timeline/revisions/{revision_id}/derive`
   */
  deriveTimelineRevision: (revision_id: string) =>
    request<TimelineRevisionResponse>(`/api/timeline/revisions/${encodeURIComponent(String(revision_id))}/derive`, { method: 'POST' }),
  /**
   * 渲染清单（合法静音必须前置声明）
   *
   * `GET /api/timeline/revisions/{revision_id}/manifest`
   */
  getTimelineManifest: (revision_id: string) =>
    request<RenderManifestResponse>(`/api/timeline/revisions/${encodeURIComponent(String(revision_id))}/manifest`, { method: 'GET' }),
  /**
   * 合成预检（含 compose_fingerprint）
   *
   * `GET /api/timeline/revisions/{revision_id}/preflight`
   */
  preflightTimelineRevision: (revision_id: string) =>
    request<PreflightResponse>(`/api/timeline/revisions/${encodeURIComponent(String(revision_id))}/preflight`, { method: 'GET' }),
  /**
   * 按**冻结计划**渲染（只消费 plan，缺任何一镜即 fail-closed）
   *
   * `POST /api/timeline/revisions/{revision_id}/render`
   */
  renderTimelineRevision: (revision_id: string, body?: RenderRequest) =>
    request<RenderVersionResponse>(`/api/timeline/revisions/${encodeURIComponent(String(revision_id))}/render`, { method: 'POST', body: JSON.stringify(body) }),
  /**
   * 列出该 revision 的成片登记
   *
   * `GET /api/timeline/revisions/{revision_id}/renders`
   */
  listTimelineRenderVersions: (revision_id: string) =>
    request<RenderVersionListResponse>(`/api/timeline/revisions/${encodeURIComponent(String(revision_id))}/renders`, { method: 'GET' }),
  /**
   * 核验渲染结果与时长（不登记 EpisodeRenderVersion）
   *
   * `POST /api/timeline/revisions/{revision_id}/verify-render`
   */
  verifyTimelineRender: (revision_id: string, body: VerifyRenderRequest) =>
    request<VerifyRenderResponse>(`/api/timeline/revisions/${encodeURIComponent(String(revision_id))}/verify-render`, { method: 'POST', body: JSON.stringify(body) }),
};
