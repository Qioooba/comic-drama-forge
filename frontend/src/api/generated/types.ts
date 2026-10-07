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


/** 契约类型（由 OpenAPI components/schemas 生成） */

/**
 * @see components/schemas/ApprovalView
 */
export interface ApprovalView {
  approved_at?: string;
  approver?: string;
  note?: string;
  reason?: string;
  state: 'none' | 'valid' | 'stale' | 'revoked' | 'hash_missing';
}

/**
 * @see components/schemas/ApproveDeliveryPackageRequest
 */
export interface ApproveDeliveryPackageRequest {
  /** 批准人；为空一律拒绝 */
  approver: string;
  note?: string;
  /** 可选：批准时确认的包摘要，与当前不一致则拒绝 */
  package_hash?: string;
}

/**
 * 批准放行。人工决定；`authorized_by` 必须是人工主体，机器账号一律 403。必须先采用过（否则 409）。
 * @see components/schemas/ApproveMediaRequest
 */
export interface ApproveMediaRequest {
  /** 审计引用号；不是身份凭证，不能覆盖主体校验 */
  authorization_ref?: string;
  authorized_by: string;
  note?: string;
}

/**
 * @see components/schemas/Attempt
 */
export interface Attempt {
  attempt_id: string;
  finished_at?: string;
  job_id: string;
  seq?: number;
  started_at?: string;
  status?: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  /** ComfyUI 工作流指纹；一致即可免重渲复用产物。 */
  workflow_hash?: string;
}

/**
 * @see components/schemas/AttemptResponse
 */
export interface AttemptResponse {
  item?: Attempt;
  success: true;
}

/**
 * @see components/schemas/BrandKit
 */
export interface BrandKit {
  brand_id: string;
  name: string;
  /** 品牌与水印的**版本**列表；指定不存在的版本会被门禁拦下 */
  versions?: Record<string, unknown>[];
}

/**
 * @see components/schemas/BrandKitsResponse
 */
export interface BrandKitsResponse {
  brands: BrandKit[];
  count: number;
  success: boolean;
  summary?: LicenseRegistrySummary;
}

/**
 * @see components/schemas/CapabilityProfileListResponse
 */
export interface CapabilityProfileListResponse {
  items: CapabilityProfileVersion[];
  success: true;
  total?: number;
}

/**
 * @see components/schemas/CapabilityProfileRequest
 */
export interface CapabilityProfileRequest {
  capabilities?: Record<string, unknown>[];
  profile_id: string;
  verified?: boolean;
  version: string;
}

/**
 * @see components/schemas/CapabilityProfileResponse
 */
export interface CapabilityProfileResponse {
  item?: CapabilityProfileVersion;
  success: true;
}

/**
 * @see components/schemas/CapabilityProfileVersion
 */
export interface CapabilityProfileVersion {
  capabilities?: Record<string, unknown>[];
  created_at?: string;
  profile_id: string;
  verified?: boolean;
  version: string;
}

/**
 * @see components/schemas/ContractVersion
 */
export interface ContractVersion {
  contract_version: string;
  openapi?: string;
  operation_count?: number;
  path_count?: number;
  /** 规格内容摘要；前端启动闸门据此判断客户端是否过期 */
  spec_hash: string;
}

/**
 * @see components/schemas/ContractVersionResponse
 */
export interface ContractVersionResponse {
  contract_version: string;
  manual_operations?: number;
  manual_paths?: string[];
  openapi?: string;
  operation_count?: number;
  path_count?: number;
  reflected_operations?: number;
  spec_hash: string;
  success: boolean;
}

/**
 * @see components/schemas/CreateAttemptRequest
 */
export interface CreateAttemptRequest {
  note?: string;
  workflow_hash?: string;
}

/**
 * @see components/schemas/CreateDeliveryPackageRequest
 */
export interface CreateDeliveryPackageRequest {
  episode_no?: number | unknown;
  /** portrait_9x16 / landscape_16x9 / portrait_3x4 */
  preset_id: string;
  project_name: string;
  requirement?: LicenseRequirement;
}

/**
 * @see components/schemas/CreateIntentRequest
 */
export interface CreateIntentRequest {
  created_by?: string;
  derived_from?: string;
  episode: string | number;
  kind?: string;
  negative_prompt?: string;
  profile_id?: string;
  profile_version?: string | number;
  project: string;
  prompt: string;
  ref_slots?: Record<string, unknown>[];
  seed?: number;
  shot_key: string;
  workflow_hash?: string;
  workflow_version?: string;
}

/**
 * @see components/schemas/CreateJobRequest
 */
export interface CreateJobRequest {
  kind: string;
  payload?: Record<string, unknown>;
  project: string;
}

/**
 * @see components/schemas/CreateTimelineRevisionRequest
 */
export interface CreateTimelineRevisionRequest {
  derived_from?: string;
  episode?: number;
  items: TimelineItem[];
  project: string;
  subtitle_revision?: string;
}

/**
 * @see components/schemas/DecisionListResponse
 */
export interface DecisionListResponse {
  items: DecisionResponse[];
  success: true;
  total?: number;
}

/**
 * @see components/schemas/DecisionRecord
 */
export interface DecisionRecord {
  actor?: string;
  approval_id?: string;
  /** 批准绑定的三元组哈希；内容变则批准自动失效 */
  bound_hash?: string;
  created_at?: string;
  kind?: 'selection' | 'approval';
  media_version_id?: string;
  note?: string;
  revoked?: boolean;
  selection_id?: string;
}

/**
 * @see components/schemas/DecisionResponse
 */
export interface DecisionResponse {
  approval?: DecisionRecord;
  /** 是否真的按磁盘现状核验过。false = 无法验证，**不得**当成「没变」。 */
  media_verified?: boolean;
  selection?: DecisionRecord;
  /** 采用/批准五态。铁律：selected 永不显示为 approved（ADR-0002） */
  state: 'selected' | 'selected_not_approved' | 'approved' | 'approved_stale' | 'approved_unverified' | 'none';
  success: true;
}

/**
 * @see components/schemas/DeliveryFile
 */
export interface DeliveryFile {
  mtime?: string;
  /** 相对交付根目录的路径；跨机器可移植 */
  rel_path: string;
  /** 文件内容摘要；内容变则摘要变 */
  sha256: string;
  size_bytes: number;
}

/**
 * @see components/schemas/DeliveryManifest
 */
export interface DeliveryManifest {
  approval?: ApprovalView;
  current_package_hash?: string;
  disk_matches_baseline?: boolean;
  file_count?: number;
  files: DeliveryFile[];
  generated_at?: string;
  licensing_ok?: boolean;
  note?: string;
  package_hash: string;
  package_id: string;
  preset?: DeliveryPreset;
  project?: string;
  schema_version?: number;
  status?: string;
  total_bytes?: number;
  verified_ok?: boolean;
}

/**
 * @see components/schemas/DeliveryManifestResponse
 */
export interface DeliveryManifestResponse {
  manifest: DeliveryManifest;
  success: boolean;
}

/**
 * @see components/schemas/DeliveryPackage
 */
export interface DeliveryPackage {
  approval?: ApprovalView;
  created_at?: string;
  /** 按磁盘现状重算的包摘要；与 package_hash 不一致即说明文件被改动过 */
  current_package_hash?: string;
  /** 磁盘现状是否仍等于清单基线 */
  disk_matches_baseline?: boolean;
  /** 清单里的文件是否都还在 */
  disk_ok?: boolean;
  episode_no?: number | unknown;
  file_count?: number;
  files?: DeliveryFile[];
  licensing_ok?: boolean;
  /** 人工批准绑定它；内容变则批准自动失效 */
  package_hash: string;
  package_id: string;
  preset?: DeliveryPreset;
  project: string;
  status: 'built' | 'verified' | 'approved' | 'approval_invalidated';
  total_bytes?: number;
  verified_at?: string;
  /** 机器校验结果；永远不等于已批准 */
  verified_ok?: boolean;
}

/**
 * @see components/schemas/DeliveryPackageListResponse
 */
export interface DeliveryPackageListResponse {
  count: number;
  packages: DeliveryPackage[];
  success: boolean;
}

/**
 * @see components/schemas/DeliveryPackageResponse
 */
export interface DeliveryPackageResponse {
  package: DeliveryPackage;
  success: boolean;
}

/**
 * @see components/schemas/DeliveryPreset
 */
export interface DeliveryPreset {
  aspect_ratio: string;
  height: number;
  /** 面向用户的中文名 */
  label: string;
  note?: string;
  orientation?: string;
  preset_id: string;
  width: number;
}

/**
 * @see components/schemas/DeliveryPresetsResponse
 */
export interface DeliveryPresetsResponse {
  count: number;
  presets: DeliveryPreset[];
  success: boolean;
}

/**
 * @see components/schemas/EpisodeRenderVersion
 */
export interface EpisodeRenderVersion {
  authorization_ref?: string;
  authorized_by?: string;
  bind_hash?: string;
  compose_fingerprint: string;
  created_at?: string;
  declared_silences?: number;
  entry_media_version_ids?: string[];
  episode?: string;
  note?: string;
  output_bytes?: number;
  output_duration_sec?: number;
  output_path?: string;
  output_sha256?: string;
  plan_fingerprint: string;
  project?: string;
  render_id: string;
  revision_id: string;
  revision_no?: number;
  subtitle_revision?: string;
}

/**
 * @see components/schemas/ErrorCodesResponse
 */
export interface ErrorCodesResponse {
  delivery: Record<string, string>;
  licensing: Record<string, string>;
  success: boolean;
}

/**
 * @see components/schemas/ErrorResponse
 */
export interface ErrorResponse {
  /** 稳定错误码，代码里以此分支，不要解析文案 */
  code: string;
  error: string;
  success: false;
}

/**
 * @see components/schemas/FinishAttemptRequest
 */
export interface FinishAttemptRequest {
  note?: string;
  result_ref?: string;
  status?: string;
}

/**
 * @see components/schemas/GateViolation
 */
export interface GateViolation {
  code: string;
  detail?: string;
  kind?: string;
  /** 面向用户的中文说明（对外契约） */
  message: string;
  /** 被拦的对象（模型名/素材 id） */
  subject?: string;
}

/**
 * 一次生成的意图。冻结后不可改，改动必须派生新 intent。
 * @see components/schemas/GenerationIntent
 */
export interface GenerationIntent {
  created_at?: string;
  derived_from?: string;
  frozen?: true;
  intent_hash: string;
  intent_id: string;
  profile?: string;
  project?: string;
  prompt?: string;
  ref_slots?: Record<string, unknown>[];
  seed?: number;
  workflow_version?: string;
}

/**
 * @see components/schemas/IntentListResponse
 */
export interface IntentListResponse {
  items: GenerationIntent[];
  success: true;
  total?: number;
}

/**
 * @see components/schemas/IntentResponse
 */
export interface IntentResponse {
  item?: GenerationIntent;
  /** 采用/批准五态。铁律：selected 永不显示为 approved（ADR-0002） */
  state?: 'selected' | 'selected_not_approved' | 'approved' | 'approved_stale' | 'approved_unverified' | 'none';
  success: true;
}

/**
 * Job = 一次用户意图；Attempt = 一次执行尝试。
 * @see components/schemas/Job
 */
export interface Job {
  created_at?: string;
  job_id: string;
  kind?: string;
  project?: string;
  status?: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  updated_at?: string;
}

/**
 * @see components/schemas/JobListResponse
 */
export interface JobListResponse {
  items: Job[];
  success: true;
  total?: number;
}

/**
 * @see components/schemas/JobResponse
 */
export interface JobResponse {
  item?: Job;
  success: true;
}

/**
 * @see components/schemas/JobStats
 */
export interface JobStats {
  by_project?: Record<string, number>;
  by_status?: Record<string, number>;
  total?: number;
}

/**
 * @see components/schemas/JobStatsResponse
 */
export interface JobStatsResponse {
  stats: JobStats;
  success: true;
}

/**
 * @see components/schemas/LicenseEntry
 */
export interface LicenseEntry {
  attribution_required?: boolean;
  /** 要求署名时，交付方复制到简介的文本 */
  attribution_text?: string;
  commercial_use?: boolean;
  id?: string;
  /** music / sfx / footage / image */
  kind?: string;
  license_type: string;
  name: string;
  notes?: string;
  source: string;
  /** false ⇒ 待核实，门禁以 LIC-LICENSE-UNVERIFIED 阻断 */
  verified?: boolean;
}

/**
 * @see components/schemas/LicenseRegistryResponse
 */
export interface LicenseRegistryResponse {
  assets: LicenseEntry[];
  brands: BrandKit[];
  /** 许可类型 → {commercial, attribution, label} */
  license_types?: Record<string, unknown>;
  models: LicenseEntry[];
  success: boolean;
  summary: LicenseRegistrySummary;
}

/**
 * @see components/schemas/LicenseRegistrySummary
 */
export interface LicenseRegistrySummary {
  asset_count?: number;
  brand_count?: number;
  load_error?: string;
  /** false 时门禁按最严口径阻断（fail-closed） */
  loaded?: boolean;
  model_count?: number;
  schema_version?: number;
  source_path?: string;
}

/**
 * 一次交付声明用到的模型与素材。门禁按它逐项核对授权登记。
 * @see components/schemas/LicenseRequirement
 */
export interface LicenseRequirement {
  assets?: string[];
  /** 交付方提供的署名文本；要求署名的授权缺它即拦 */
  attribution_text?: string;
  brand?: string;
  brand_version?: string;
  /** 模型名称，需与 config/model-licensing.json 的 name 对上 */
  models?: string[];
  music?: string[];
  purpose?: string;
}

/**
 * @see components/schemas/LicensedMusicResponse
 */
export interface LicensedMusicResponse {
  count: number;
  music: LicenseEntry[];
  success: boolean;
  summary?: LicenseRegistrySummary;
}

/**
 * @see components/schemas/LicensingGateResponse
 */
export interface LicensingGateResponse {
  /** 可复制到简介的署名文本 */
  attribution?: string[];
  gate: LicensingGateResult;
  /** 与 gate.ok 同值 */
  success: boolean;
}

/**
 * @see components/schemas/LicensingGateResult
 */
export interface LicensingGateResult {
  checked?: Record<string, unknown>;
  /** false 时不得进入交付 */
  ok: boolean;
  registry?: Record<string, unknown>;
  summary?: string;
  violations: GateViolation[];
}

/**
 * 一次生成产出的候选。自身**不带** selected/approved —— 那两个是独立的决策记录（ADR-0002）。
 * @see components/schemas/MediaVersion
 */
export interface MediaVersion {
  created_at?: string;
  ffprobe?: Record<string, unknown>;
  intent_id?: string;
  kind?: 'image' | 'video' | 'audio';
  media_sha256: string;
  media_version_id: string;
  /** 磁盘路径；批准时会按它重算 sha256 以判是否被覆盖 */
  path?: string;
}

/**
 * @see components/schemas/MediaVersionListResponse
 */
export interface MediaVersionListResponse {
  items: MediaVersionResponse[];
  success: true;
  total?: number;
}

/**
 * @see components/schemas/MediaVersionResponse
 */
export interface MediaVersionResponse {
  item?: MediaVersion;
  /** 采用/批准五态。铁律：selected 永不显示为 approved（ADR-0002） */
  state?: 'selected' | 'selected_not_approved' | 'approved' | 'approved_stale' | 'approved_unverified' | 'none';
  success: true;
}

/**
 * @see components/schemas/OkResponse
 */
export interface OkResponse {
  success: true;
}

/**
 * @see components/schemas/OpenApiSpecResponse
 */
export interface OpenApiSpecResponse {
  components?: Record<string, unknown>;
  info: Record<string, unknown>;
  openapi: string;
  paths: Record<string, unknown>;
  'x-contract-version'?: string;
  'x-manual-paths'?: string[];
}

/**
 * @see components/schemas/PendingUnit
 */
export interface PendingUnit {
  done?: boolean;
  reason?: string;
  unit_key?: string;
}

/**
 * @see components/schemas/PendingUnitListResponse
 */
export interface PendingUnitListResponse {
  items: PendingUnit[];
  success: true;
  total?: number;
}

/**
 * @see components/schemas/PreflightReport
 */
export interface PreflightReport {
  /** 内容指纹：不含 revision_id/created_at，所以「同一批镜头换机器重渲」不会误判为内容变化。 */
  compose_fingerprint: string;
  issues?: Record<string, unknown>[];
  ok: boolean;
  undeclared_silence?: Record<string, unknown>[];
}

/**
 * @see components/schemas/PreflightResponse
 */
export interface PreflightResponse {
  preflight: PreflightReport;
  success: true;
}

/**
 * 一次登记失败的台账行。生成路径不会因它中断，所以它是「失败不读日志也能发现」的唯一凭据。
 * @see components/schemas/RecordingFailure
 */
export interface RecordingFailure {
  /** 未登记成功的产物路径 */
  artifact?: string;
  attempt_id?: string;
  episode?: string;
  intent_fingerprint?: string;
  kind?: string;
  project?: string;
  reason: string;
  shot_key?: string;
  /** 调用点标识，便于定位是哪条生成路径 */
  source?: string;
  /** 失败发生的阶段（intent / media / …） */
  stage?: string;
  ts?: string;
}

/**
 * @see components/schemas/RecordingFailureListResponse
 */
export interface RecordingFailureListResponse {
  count: number;
  failures: RecordingFailure[];
}

/**
 * 服务端按 path 重算 sha256，**不接受客户端自带摘要**（否则可伪造出「已核验」的假象）。
 * @see components/schemas/RegisterMediaRequest
 */
export interface RegisterMediaRequest {
  intent_id: string;
  kind?: 'image' | 'video' | 'audio';
  path: string;
}

/**
 * 发布检查不接受客户端授权结论；服务端按 requirement 重新评估。
 * @see components/schemas/ReleaseCheckRequest
 */
export interface ReleaseCheckRequest {
  [key: string]: unknown;
}

/**
 * @see components/schemas/ReleaseCheckResponse
 */
export interface ReleaseCheckResponse {
  package?: Record<string, unknown>;
  release: Record<string, unknown>;
  success: boolean;
}

/**
 * 渲染清单。合法静音在此**前置声明**，而不是渲染后才发现。
 * @see components/schemas/RenderManifest
 */
export interface RenderManifest {
  compose_fingerprint: string;
  declared_silences?: Record<string, unknown>[];
  renderer?: string;
  segments?: Record<string, unknown>[];
  subtitle_revision?: string;
}

/**
 * @see components/schemas/RenderManifestResponse
 */
export interface RenderManifestResponse {
  manifest: RenderManifest;
  success: true;
}

/**
 * 按冻结计划发起渲染。``authorized_by`` 必须是人工主体。
 * @see components/schemas/RenderRequest
 */
export interface RenderRequest {
  authorization_ref?: string;
  /** 机器账号一律 403（复用 require_human_authorization） */
  authorized_by: string;
  /** 只做预检不真渲染；用于接上游合成计划的安全接入 */
  dry_run?: boolean;
  note?: string;
  output_path?: string;
  preset?: string;
  preview?: boolean;
  renderer?: string;
  target_fps?: number;
  target_h?: number;
  target_w?: number;
}

/**
 * @see components/schemas/RenderVersionListResponse
 */
export interface RenderVersionListResponse {
  count: number;
  renders: EpisodeRenderVersion[];
  success: true;
}

/**
 * @see components/schemas/RenderVersionResponse
 */
export interface RenderVersionResponse {
  render_version: EpisodeRenderVersion;
  success: true;
}

/**
 * @see components/schemas/ReuseHintResponse
 */
export interface ReuseHintResponse {
  /** 为何不可复用；workflow_hash 不一致 / 产物缺失 */
  reason?: string;
  reusable?: boolean;
  success: true;
  workflow_hash?: string;
}

/**
 * @see components/schemas/RevokeApprovalRequest
 */
export interface RevokeApprovalRequest {
  reason: string;
  revoked_by: string;
}

/**
 * @see components/schemas/RevokeDeliveryApprovalRequest
 */
export interface RevokeDeliveryApprovalRequest {
  operator?: string;
  reason?: string;
}

/**
 * 采用这版。创作决定 —— **不会**自动产生批准。
 * @see components/schemas/SelectMediaRequest
 */
export interface SelectMediaRequest {
  note?: string;
  selected_by?: string;
}

/**
 * 三态判别：**从未生成** ≠ **生成了但没登记**。早先界面只有「有候选/没候选」，这两种情况长得一模一样，用户只能翻日志。
 * @see components/schemas/ShotGenerationStatus
 */
export interface ShotGenerationStatus {
  /** 已进入事实库的产物路径 */
  candidate_paths?: string[];
  episode?: string;
  failures?: RecordingFailure[];
  intent_count?: number;
  intents?: GenerationIntent[];
  kind?: string;
  media_version_count?: number;
  project?: string;
  /** 恒为 false（铁律 1）。留在响应里是让调用方**按码判断**而非按文案猜 */
  selection_implies_approval: false;
  shot_key?: string;
  /** never_generated=真的还没渲过；generated_unrecorded=磁盘上有产物或台账有失败、但事实库里没有候选；recorded=有候选可比对 */
  state: 'never_generated' | 'generated_unrecorded' | 'recorded';
  /** 磁盘上存在但**没有**登记的产物路径 */
  unregistered_paths?: string[];
}

/**
 * @see components/schemas/ShotGenerationStatusResponse
 */
export interface ShotGenerationStatusResponse {
  item?: ShotGenerationStatus;
  success: true;
}

/**
 * style_id 是**稳定标识**，label 可改名而 id 永不变（否则改名即断历史，ADR-0008）。
 * @see components/schemas/StyleEntry
 */
export interface StyleEntry {
  aliases?: string[];
  aspect_default?: string;
  category: '2d' | '3d' | 'real';
  /** 写入 config.style 的字面值 */
  label: string;
  negative_suffix?: string[];
  positive_suffix?: string;
  /** 纯 ASCII，与缩略图文件名刻意解耦 */
  style_id: string;
  thumbnail?: string;
}

/**
 * @see components/schemas/StyleListResponse
 */
export interface StyleListResponse {
  aspect_presets?: Record<string, unknown>[];
  categories?: Record<string, unknown>[];
  items: StyleEntry[];
  success: true;
  total?: number;
}

/**
 * @see components/schemas/StyleResolveResponse
 */
export interface StyleResolveResponse {
  match_kind?: 'id' | 'label' | 'alias' | 'substring';
  /** false = 未命中。调用方**必须**继续按自由文本处理，不得凭空猜一个风格。 */
  resolved: boolean;
  style_id?: string;
  success: true;
}

/**
 * @see components/schemas/StyleResponse
 */
export interface StyleResponse {
  item?: StyleEntry;
  success: true;
}

/**
 * @see components/schemas/TimelineItem
 */
export interface TimelineItem {
  audio_media_version_id?: string;
  /** 合法静音必须**前置声明**。未声明却在渲染后出现静音 → 阻断。 */
  declared_silence?: boolean;
  duration_sec?: number;
  index?: number;
  /** 采用的媒体版本；引用未批准/已失效的版本会阻断 */
  media_version_id: string;
  note?: string;
  shot_key: string;
  silence_reason?: string;
  transition_in?: string;
}

/**
 * 不可变时间线。创建后不可原地改；改内容必须派生新 revision。
 * @see components/schemas/TimelineRevision
 */
export interface TimelineRevision {
  created_at?: string;
  derived_from?: string;
  episode?: number;
  items?: TimelineItem[];
  project: string;
  revision_id: string;
  revision_no?: number;
  /** 字幕版本与时间线解耦，仅在此引用 */
  subtitle_revision?: string;
}

/**
 * @see components/schemas/TimelineRevisionListResponse
 */
export interface TimelineRevisionListResponse {
  items: TimelineRevision[];
  success: true;
  total?: number;
}

/**
 * @see components/schemas/TimelineRevisionResponse
 */
export interface TimelineRevisionResponse {
  item?: TimelineRevision;
  success: true;
}

/**
 * @see components/schemas/UpdateAttemptRequest
 */
export interface UpdateAttemptRequest {
  note?: string;
  status: string;
}

/**
 * @see components/schemas/UpdateJobRequest
 */
export interface UpdateJobRequest {
  note?: string;
  status: string;
}

/**
 * @see components/schemas/VerifyDeliveryResponse
 */
export interface VerifyDeliveryResponse {
  package: DeliveryPackage;
  /** 与 verify.ok 同值：机器校验结论 */
  success: boolean;
  verify: VerifyResult;
}

/**
 * 渲完结果核验：传 ffprobe 结果，不登记 EpisodeRenderVersion。
 * @see components/schemas/VerifyRenderRequest
 */
export interface VerifyRenderRequest {
  probed: Record<string, unknown>;
  tolerance_sec?: number;
}

/**
 * @see components/schemas/VerifyRenderResponse
 */
export interface VerifyRenderResponse {
  compose_fingerprint: string;
  ok: boolean;
  planned_duration_sec: number;
  reason: string;
  success: true;
}

/**
 * @see components/schemas/VerifyResult
 */
export interface VerifyResult {
  checked: number;
  details?: Record<string, unknown>[];
  mismatched?: Record<string, unknown>[];
  missing?: string[];
  ok: boolean;
  violations: GateViolation[];
}
