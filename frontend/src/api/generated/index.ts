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


export * from './types';
export {
  contractsApi,
  deliveryApi,
  jobsApi,
  licensingApi,
  productionfactsApi,
  stylesApi,
  timelineApi,
} from './client';

/** 契约版本与规格摘要 —— 前端启动闸门拿它判断客户端是否过期 */
export const CONTRACT_VERSION = '2026-10-07.2';
export const SPEC_HASH = '4c0774a9aeeb4d02b3b575f8feaab6e81de804aedc0785a44a1d846a45e6c2b9';

/** 生成物内可用的类型名（供泛型标注与测试引用） */
export type GeneratedTypeName =
  | 'ApprovalView'
  | 'ApproveDeliveryPackageRequest'
  | 'ApproveMediaRequest'
  | 'Attempt'
  | 'AttemptResponse'
  | 'BrandKit'
  | 'BrandKitsResponse'
  | 'CapabilityProfileListResponse'
  | 'CapabilityProfileRequest'
  | 'CapabilityProfileResponse'
  | 'CapabilityProfileVersion'
  | 'ContractVersion'
  | 'ContractVersionResponse'
  | 'CreateAttemptRequest'
  | 'CreateDeliveryPackageRequest'
  | 'CreateIntentRequest'
  | 'CreateJobRequest'
  | 'CreateTimelineRevisionRequest'
  | 'DecisionListResponse'
  | 'DecisionRecord'
  | 'DecisionResponse'
  | 'DeliveryFile'
  | 'DeliveryManifest'
  | 'DeliveryManifestResponse'
  | 'DeliveryPackage'
  | 'DeliveryPackageListResponse'
  | 'DeliveryPackageResponse'
  | 'DeliveryPreset'
  | 'DeliveryPresetsResponse'
  | 'EpisodeRenderVersion'
  | 'ErrorCodesResponse'
  | 'ErrorResponse'
  | 'FinishAttemptRequest'
  | 'GateViolation'
  | 'GenerationIntent'
  | 'IntentListResponse'
  | 'IntentResponse'
  | 'Job'
  | 'JobListResponse'
  | 'JobResponse'
  | 'JobStats'
  | 'JobStatsResponse'
  | 'LicenseEntry'
  | 'LicenseRegistryResponse'
  | 'LicenseRegistrySummary'
  | 'LicenseRequirement'
  | 'LicensedMusicResponse'
  | 'LicensingGateResponse'
  | 'LicensingGateResult'
  | 'MediaVersion'
  | 'MediaVersionListResponse'
  | 'MediaVersionResponse'
  | 'OkResponse'
  | 'OpenApiSpecResponse'
  | 'PendingUnit'
  | 'PendingUnitListResponse'
  | 'PreflightReport'
  | 'PreflightResponse'
  | 'RegisterMediaRequest'
  | 'ReleaseCheckRequest'
  | 'ReleaseCheckResponse'
  | 'RenderManifest'
  | 'RenderManifestResponse'
  | 'RenderRequest'
  | 'RenderVersionListResponse'
  | 'RenderVersionResponse'
  | 'ReuseHintResponse'
  | 'RevokeApprovalRequest'
  | 'RevokeDeliveryApprovalRequest'
  | 'SelectMediaRequest'
  | 'StyleEntry'
  | 'StyleListResponse'
  | 'StyleResolveResponse'
  | 'StyleResponse'
  | 'TimelineItem'
  | 'TimelineRevision'
  | 'TimelineRevisionListResponse'
  | 'TimelineRevisionResponse'
  | 'UpdateAttemptRequest'
  | 'UpdateJobRequest'
  | 'VerifyDeliveryResponse'
  | 'VerifyRenderRequest'
  | 'VerifyRenderResponse'
  | 'VerifyResult';
