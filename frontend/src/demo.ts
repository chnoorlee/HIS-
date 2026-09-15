import type { AuditEntry, Encounter, Fact, Job, Note, Session, Source, Transcript, User } from "./types";

// These authored fixtures are independent of any local or hospital database.
const timestamp = "2026-09-15T08:30:00+08:00";
export const demoUser: User = {
  id: "public-demo-visitor", username: "public-demo", display_name: "演示访客",
  roles: ["doctor", "admin"], hospital_id: "hospital_demo",
};
const encounters: Encounter[] = [
  { id: "demo-enc-001", patient_name: "李明（模拟）", patient_id: "DEMO-P001", admission_id: "DEMO-A001", bed: "08", department: "呼吸与危重症医学科", age: 62, sex: "男", diagnosis: "咳嗽、发热待查（虚构病例）", admitted_at: timestamp },
  { id: "demo-enc-002", patient_name: "陈芳（模拟）", patient_id: "DEMO-P002", admission_id: "DEMO-A002", bed: "12", department: "消化内科", age: 48, sex: "女", diagnosis: "腹痛待查（虚构病例）", admitted_at: timestamp },
  { id: "demo-enc-003", patient_name: "王建国（模拟）", patient_id: "DEMO-P003", admission_id: "DEMO-A003", bed: "15", department: "心血管内科", age: 71, sex: "男", diagnosis: "胸闷待查（虚构病例）", admitted_at: timestamp },
];
const histories = [
  ["咳嗽3天，伴发热1天。", "患者自述3天前出现咳嗽，昨晚自觉发热，具体体温未测。", "患者自述既往有高血压，具体用药名称及剂量待核实。"],
  ["间断上腹痛2天。", "患者自述2天前出现间断上腹不适，与进食的关系尚未明确。", "患者自述曾有类似腹痛，既往检查报告尚未获取。"],
  ["活动后胸闷1周。", "患者自述近1周活动后出现胸闷，休息后缓解，持续时间待进一步核实。", "患者自述有高血压病史，既往治疗经过待核实。"],
];
const admissionChapters = [
  ["general", "一般资料"], ["chief_complaint", "主诉"], ["history_present", "现病史"],
  ["past_history", "既往史"], ["personal_history", "个人史"], ["marital_history", "婚育史"],
  ["family_history", "家族史"], ["allergies", "过敏史"], ["medication_history", "用药史"],
  ["physical_exam", "体格检查"], ["specialist_exam", "专科检查"], ["investigations", "辅助检查"],
  ["diagnosis", "初步诊断"], ["plan", "诊疗计划"],
];
const sessions: Session[] = [];
const notes: Note[] = [];
const transcripts = new Map<string, Transcript[]>();
const facts = new Map<string, Fact[]>();
const sources = new Map<string, Source[]>();
const jobs = new Map<string, Job[]>();
const audit: AuditEntry[] = [];

encounters.forEach((encounter, index) => {
  const sessionId = `demo-session-00${index + 1}`;
  sessions.push({ id: sessionId, encounter_id: encounter.id, status: "DEMO", mode: "conversation", created_at: timestamp, revision: 1 });
  const script: Transcript[] = [
    { id: `${sessionId}-t1`, text: "这次主要是哪里不舒服？从什么时候开始的？", speaker: "doctor", subject: "patient", section: "history_present", revision: 1, created_at: timestamp, source_type: "synthetic_script" },
    { id: `${sessionId}-t2`, text: histories[index][1], speaker: "patient", subject: "patient", section: "history_present", revision: 1, created_at: timestamp, source_type: "synthetic_script" },
    { id: `${sessionId}-t3`, text: histories[index][2], speaker: "patient", subject: "patient", section: "past_history", revision: 1, created_at: timestamp, source_type: "synthetic_script" },
    { id: `${sessionId}-t4`, text: "家属称患者以前可能有药物过敏，但药名和反应记不清。", speaker: "family", subject: "patient", section: "allergies", revision: 1, created_at: timestamp, source_type: "synthetic_script" },
    { id: `${sessionId}-t5`, text: "我自己有糖尿病，患者的情况我不清楚。", speaker: "family", subject: "family", section: "family_history", revision: 1, created_at: timestamp, source_type: "synthetic_script" },
  ];
  transcripts.set(sessionId, script);
  const sessionFacts: Fact[] = script.slice(1).map((item, factIndex) => ({
    id: `${sessionId}-f${factIndex + 1}`, text: item.text, concept: "陈述信息", section: item.section!,
    subject: item.subject, speaker: item.speaker, polarity: "positive", certainty: factIndex < 2 ? "certain" : "uncertain",
    elicitation: "asked", conflict_status: factIndex === 2 ? "unresolved" : "none",
    confirmation_status: factIndex === 3 ? "excluded" : factIndex === 0 ? "confirmed" : "unconfirmed",
    source_ids: [item.id], evidence: [{ source_id: item.id, source_revision: 1, source_type: "synthetic_script", synthetic: true }], revision: 1,
  }));
  facts.set(sessionId, sessionFacts);
  sources.set(encounter.id, [
    { id: `${encounter.id}-source1`, title: "入院登记（虚构）", source_system: "演示资料", version: 1, status: "AVAILABLE", clinical_time: timestamp, content: { 姓名: encounter.patient_name, 住院号: encounter.admission_id, 科室: encounter.department, 资料性质: "人工编写的虚构样例，未连接院内系统" } },
    { id: `${encounter.id}-source2`, title: "既往就诊摘要（虚构）", source_system: "演示资料", version: 2, status: "AVAILABLE", clinical_time: timestamp, content: { 病史摘要: histories[index][2], 用药信息: "药名、剂量与频次尚待核实", 资料性质: "仅展示来源版本，不能用于诊疗" } },
  ]);
  const body: Record<string, string> = {
    general: `${encounter.patient_name}，${encounter.sex}，${encounter.age}岁。病史由患者及家属提供。以下内容均为虚构演示资料。`,
    chief_complaint: histories[index][0], history_present: histories[index][1], past_history: histories[index][2],
    personal_history: "吸烟、饮酒及职业暴露史尚未询问。", marital_history: "婚育情况尚未获取。", family_history: "家族史尚未核实。家属自身疾病陈述未作为患者既往史。",
    allergies: "家属提供疑似药物过敏史，具体药物及反应待核实；不能记录为无过敏史。",
    medication_history: "当前用药名称、剂量及频次待核实。", physical_exam: "尚未录入医生实际查体结果。", specialist_exam: "尚未录入专科检查结果。",
    investigations: "尚未获取本次就诊检查结果。", diagnosis: encounter.diagnosis, plan: "待医生结合实际问诊、查体与检查结果制定。",
  };
  const chapterSets: [string, string[][]][] = [
    ["admission", admissionChapters],
    ["first_progress", [["case_features", "病例特点"], ["diagnostic_analysis", "诊断依据与鉴别分析"], ["plan", "诊疗计划"]]],
    ["daily_progress", [["interval_history", "病情变化"], ["physical_exam", "体格检查"], ["investigations", "辅助检查"], ["assessment", "病情评估"], ["plan", "诊疗计划"]]],
    ["discharge", [["admission_diagnosis", "入院诊断"], ["discharge_diagnosis", "出院诊断"], ["hospital_course", "住院经过"], ["discharge_condition", "出院情况"], ["discharge_medications", "出院用药"], ["follow_up", "随访安排"]]],
  ];
  chapterSets.forEach(([noteType, chapters]) => {
    const blocks = chapters.map(([key, title]) => {
      const linked = sessionFacts.filter(fact => fact.section === key && fact.subject === "patient" && fact.confirmation_status !== "excluded");
      return { key, title, text: body[key] ?? (key === "case_features" ? histories[index][1] : "尚未获取相关信息，待医生如实补充。"), fact_ids: linked.map(fact => fact.id), facts_snapshot: Object.fromEntries(linked.map(fact => [fact.id, fact.revision])) };
    });
    notes.push({ id: `${encounter.id}-${noteType}`, encounter_id: encounter.id, session_id: sessionId, note_type: noteType, status: "REVIEW_REQUIRED", revision: 2, blocks,
      issues: [{ id: `${encounter.id}-issue`, code: "unverified_information", message: "虚构样例：过敏信息与用药信息尚待医生核实。", severity: "blocker", block_key: noteType === "admission" ? "allergies" : chapters[0][0] }],
      created_at: timestamp, revisions: [{ revision: 1, created_at: timestamp, blocks: blocks.map(block => ({ ...block, text: block.key === "chief_complaint" ? `${encounter.diagnosis}，病程待核实（虚构初稿）。` : block.text })) }, { revision: 2, created_at: timestamp, blocks }],
    });
  });
  jobs.set(encounter.id, [{ id: `${encounter.id}-job1`, kind: "章节整理（虚构样例）", status: "SUCCEEDED", created_at: timestamp, result: { synthetic: true, model_called: false, description: "预置静态结果，未经真实大模型处理" } }]);
  audit.push({ id: `${encounter.id}-audit1`, action: "demo.fixture.loaded", actor_id: "演示样例", entity_id: encounter.id, created_at: timestamp, details: { synthetic: true } });
});

const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
export function demoResponse(path: string, options: RequestInit = {}): Response {
  if ((options.method ?? "GET").toUpperCase() !== "GET")
    return response({ detail: { code: "public_demo_read_only", message: "公开演示为只读，未执行保存、采音、模型调用或院内写回。" } }, 403);
  const [pathname, queryString = ""] = path.split("?");
  const query = new URLSearchParams(queryString);
  const parts = pathname.split("/").filter(Boolean);
  if (pathname === "/auth/config") return response({ environment: "pages", mode: "local" });
  if (pathname === "/auth/me") return response(demoUser);
  if (pathname === "/system/status") return response({ mode: "public_demo", environment: "GitHub Pages · 只读静态演示", synthetic: true, asr_provider: "未连接", llm_provider: "未连接", connector: "未连接 HIS / EMR", fact_extraction_ready: false, audio_capture: "已禁用", persistence: "无后端存储", author: "Yongzhi Li (chnoorlee)", repository: "https://github.com/chnoorlee/HIS-" });
  if (pathname === "/encounters") {
    const search = (query.get("search") ?? "").toLowerCase();
    return response(encounters.filter(item => `${item.patient_name} ${item.bed} ${item.admission_id}`.toLowerCase().includes(search)));
  }
  if (parts[0] === "encounters" && parts[2] === "sources" && parts.length === 3) return response(sources.get(parts[1]) ?? []);
  if (pathname === "/sessions") return response(sessions.filter(item => item.encounter_id === query.get("encounter_id")));
  if (parts[0] === "sessions" && parts.length === 2) {
    const session = sessions.find(item => item.id === parts[1]);
    if (session) return response(session);
  }
  if (parts[0] === "sessions" && parts[2] === "transcripts" && parts.length === 3) return response(transcripts.get(parts[1]) ?? []);
  if (parts[0] === "sessions" && parts[2] === "facts" && parts.length === 3) return response(facts.get(parts[1]) ?? []);
  if (pathname === "/notes") return response(notes.filter(item => item.encounter_id === query.get("encounter_id")));
  if (parts[0] === "notes" && parts.length === 2) {
    const note = notes.find(item => item.id === parts[1]);
    if (note) return response(note);
  }
  if (pathname === "/jobs") return response(jobs.get(query.get("encounter_id") ?? "") ?? []);
  if (pathname === "/exports") return response([]);
  if (pathname === "/audit") return response(audit);
  if (pathname === "/admin/settings") return response({ export_enabled: false, recovery_isolation: true });
  if (pathname === "/admin/incidents") return response([]);
  return response({ detail: { code: "public_demo_unavailable", message: "此功能需要独立部署的院内服务，公开演示未连接后端。" } }, 404);
}
