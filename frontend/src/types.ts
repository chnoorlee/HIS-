export interface User {
  id: string;
  username: string;
  display_name?: string;
  roles: string[];
  hospital_id: string;
}
export interface Encounter {
  id: string;
  patient_name: string;
  patient_id: string;
  admission_id: string;
  bed: string;
  department: string;
  age: number;
  sex: string;
  diagnosis: string;
  admitted_at: string;
}
export interface Session {
  id: string;
  encounter_id: string;
  status: string;
  mode: string;
  created_at: string;
  revision: number;
}
export interface Transcript {
  id: string;
  text: string;
  speaker: string;
  subject: string;
  section?: string;
  revision: number;
  created_at: string;
  audio_range?: Record<string, unknown>;
  source_type?: string;
}
export interface Fact {
  id: string;
  text: string;
  concept: string;
  section: string;
  subject: string;
  speaker: string;
  polarity: string;
  certainty: string;
  elicitation: string;
  conflict_status: string;
  confirmation_status: string;
  source_ids: string[];
  evidence: Record<string, unknown>[];
  revision: number;
}
export interface Source {
  id: string;
  title?: string;
  source_system?: string;
  source_type?: string;
  version?: string | number;
  status?: string;
  content?: unknown;
  data?: unknown;
  created_at?: string;
  clinical_time?: string;
}
export interface Block {
  key: string;
  title: string;
  text: string;
  fact_ids: string[];
  facts_snapshot?: Record<string, number>;
  author?: string;
  protected?: boolean;
}
export interface ReferenceReview {
  fact_revisions: Record<string, number>;
  reason: string;
}
export interface Issue {
  id: string;
  code: string;
  message: string;
  severity: string;
  block_key?: string;
  fact_id?: string;
  resolvable?: boolean;
}
export interface Note {
  id: string;
  encounter_id: string;
  session_id: string;
  note_type: string;
  status: string;
  revision: number;
  blocks: Block[];
  issues: Issue[];
  review_id?: string | null;
  revisions?: { revision: number; created_at?: string; blocks?: Block[] }[];
  created_at: string;
}
export interface Review {
  id: string;
  note_id: string;
  note_revision: number;
  digest: string;
}
export interface ExportOperation {
  id: string;
  status: string;
  note_id?: string;
  note_revision?: number;
  external_id?: string;
  remote_document_id?: string;
  error?: string;
  message?: string;
  created_at?: string;
}
export interface Job {
  id: string;
  status: string;
  kind?: string;
  job_type?: string;
  error?: string;
  created_at?: string;
  result?: unknown;
}
export interface AuditEntry {
  id: string;
  action: string;
  actor_id?: string;
  user_id?: string;
  entity_type?: string;
  entity_id?: string;
  created_at: string;
  details?: unknown;
}
export interface SystemStatus {
  mode?: string;
  environment?: string;
  provider?: unknown;
  asr_provider?: string;
  llm_provider?: string;
  connector?: unknown;
  [key: string]: unknown;
}
export const NOTE_TYPES: Record<string, string> = {
  admission: "入院记录",
  first_progress: "首次病程",
  daily_progress: "日常病程",
  discharge: "出院记录",
};
export const ROLE_NAMES: Record<string, string> = {
  doctor: "医生",
  patient: "患者",
  family: "家属",
  unknown: "未确认",
  other: "其他",
};
export const STATUS_NAMES: Record<string, string> = {
  CREATED: "未开始",
  RECORDING: "录音中",
  PAUSED: "已暂停",
  FINALIZING: "正在归档",
  COMPLETE: "音频完整",
  INCOMPLETE: "音频待补全",
  QUARANTINED: "已隔离",
  DRAFT: "草稿",
  REVIEW_REQUIRED: "待复核",
  REVIEWED: "已审核",
  SUPERSEDED: "已有新版本",
  PENDING: "排队中",
  RUNNING: "处理中",
  SUCCEEDED: "已完成",
  COMPLETED: "已完成",
  FAILED: "失败",
  UNKNOWN: "结果未知",
  SUCCEEDED_VERIFIED: "已回读核验",
  VERIFIED: "已回读核验",
  AVAILABLE: "可用",
  CONFIRMED: "已确认",
};
export const statusName = (s?: string) =>
  STATUS_NAMES[s?.toUpperCase() ?? ""] ?? s ?? "未知";
