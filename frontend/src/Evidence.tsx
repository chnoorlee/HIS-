import { useEffect, useRef, useState } from "react";
import {
  AudioLines,
  BookOpen,
  Check,
  ChevronRight,
  CircleHelp,
  FileText,
  Link2,
  MessageSquareText,
  Pencil,
  Plus,
  Save,
  Sparkles,
  X,
} from "lucide-react";
import { api, authenticatedFetch, isCancelled, patch, post, tokenStore } from "./api";
import { useSessionEvents } from "./useSessionEvents";
import {
  Empty,
  IconButton,
  Loading,
  Modal,
  Notice,
  StructuredData,
  Status,
} from "./components";
import { ROLE_NAMES, type Fact, type Job, type Source, type Transcript } from "./types";
import { PUBLIC_DEMO } from "./mode";
const SECTION_NAMES: Record<string, string> = {
  chief_complaint: "主诉",
  history_present: "现病史",
  past_history: "既往史",
  allergies: "过敏史",
  physical_exam: "体格检查",
  assessment: "诊断分析",
  plan: "诊疗计划",
  diagnosis: "初步诊断",
  investigations: "辅助检查",
  case_features: "病例特点",
  diagnostic_analysis: "诊断依据与鉴别分析",
  interval_history: "病情变化",
  admission_diagnosis: "入院诊断",
  discharge_diagnosis: "出院诊断",
  hospital_course: "住院经过",
  discharge_condition: "出院情况",
  discharge_medications: "出院用药",
  follow_up: "随访安排",
};
export default function Evidence({
  sessionId,
  extractionReady,
  transcripts,
  facts,
  sources,
  onRefresh,
  onError,
  selectedFact,
}: {
  sessionId?: string;
  extractionReady: boolean;
  transcripts: Transcript[];
  facts: Fact[];
  sources: Source[];
  onRefresh: () => Promise<void>;
  onError: (error: unknown) => void;
  selectedFact?: { id: string; sessionId: string };
}) {
  const [tab, setTab] = useState("transcripts");
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Transcript | null>(null);
  const [text, setText] = useState("");
  const [speaker, setSpeaker] = useState("doctor");
  const [subject, setSubject] = useState("patient");
  const [section, setSection] = useState("history_present");
  const [busy, setBusy] = useState(false);
  const [fact, setFact] = useState<Fact | null>(null);
  const [factLoading, setFactLoading] = useState(false);
  const factRequest = useRef<AbortController | null>(null);
  const [resolutionReason, setResolutionReason] = useState('');
  const [source, setSource] = useState<Source | null>(null);
  const [audioUrl, setAudioUrl] = useState("");
  const [audioLoading, setAudioLoading] = useState(false);
  const audioUrlRef = useRef("");
  const audioRequest = useRef<AbortController | null>(null);
  const [extractionJob, setExtractionJob] = useState<Job | null>(null);
  const { partials, connection } = useSessionEvents(sessionId, onRefresh, onError);
  const extracting = Boolean(extractionJob && ["PENDING", "RUNNING", "QUEUED", "RETRY"].includes(extractionJob.status));
  useEffect(() => {
    if (!extracting || !extractionJob) return;
    let cancelled = false;
    const timer = setInterval(() => {
      void api<Job>(`/jobs/${extractionJob.id}`).then(async (current) => {
        if (cancelled) return;
        setExtractionJob(current);
        if (["SUCCEEDED", "COMPLETED", "FAILED"].includes(current.status)) {
          await onRefresh();
          if (current.status === "FAILED") onError(new Error(current.error ?? "临床事实提取失败"));
        }
      }).catch(onError);
    }, 1800);
    return () => { cancelled = true; clearInterval(timer); };
  }, [extracting, extractionJob?.id, onRefresh, onError]);
  useEffect(() => {
    if (!selectedFact) return;
    const controller = new AbortController();
    factRequest.current = controller;
    setTab("facts");
    setResolutionReason("");
    setFact(null);
    setFactLoading(true);
    void api<Fact[]>(`/sessions/${selectedFact.sessionId}/facts`, { signal: controller.signal }).then((current) => {
      if (controller.signal.aborted) return;
      const found = current.find((item) => item.id === selectedFact.id);
      if (found) setFact({ ...found });
      else onError(new Error("当前来源事实不可用，请刷新后核查。"));
    }).catch((error) => { if (!controller.signal.aborted && !isCancelled(error)) onError(error); })
      .finally(() => { if (!controller.signal.aborted) setFactLoading(false); });
    return () => controller.abort();
  }, [selectedFact, onError]);
  useEffect(() => {
    setAdding(false);
    setEditing(null);
    setFact(null);
    setSource(null);
    setAudioUrl("");
    setAudioLoading(false);
    setExtractionJob(null);
    audioRequest.current?.abort();
    if (audioUrlRef.current) URL.revokeObjectURL(audioUrlRef.current);
    audioUrlRef.current = "";
  }, [sessionId]);
  useEffect(
    () => () => {
      if (audioUrlRef.current) URL.revokeObjectURL(audioUrlRef.current);
      audioRequest.current?.abort();
    },
    [],
  );
  async function run(action: () => Promise<void>) {
    setBusy(true);
    try {
      await action();
    } catch (error) {
      onError(error);
    } finally {
      setBusy(false);
    }
  }
  function editTranscript(value?: Transcript) {
    setEditing(value ?? null);
    setText(value?.text ?? "");
    setSpeaker(value?.speaker ?? "doctor");
    setSubject(value?.subject ?? "patient");
    setSection(value?.section ?? "history_present");
    setAdding(true);
  }
  async function saveTranscript() {
    if (!sessionId || !text.trim()) return;
    await run(async () => {
      if (editing)
        await patch(`/transcripts/${editing.id}`, {
          base_revision: editing.revision,
          text,
          speaker,
          subject,
        });
      else
        await post(`/sessions/${sessionId}/transcripts`, {
          text,
          speaker,
          subject,
          section,
        });
      setAdding(false);
      await onRefresh();
    });
  }
  async function saveFact() {
    if (!fact) return;
    await run(async () => {
      await patch(`/facts/${fact.id}`, {
        base_revision: fact.revision,
        text: fact.text,
        subject: fact.subject,
        speaker: fact.speaker,
        polarity: fact.polarity,
        certainty: fact.certainty,
        elicitation: fact.elicitation,
        conflict_status: fact.conflict_status,
        confirmation_status: fact.confirmation_status,
        ...(resolutionReason.trim() ? {resolution_reason:resolutionReason}:{}),
      });
      setFact(null);
      await onRefresh();
    });
  }
  async function playTranscript(transcript: Transcript) {
    if (!sessionId || !transcript.audio_range) return;
    setAudioLoading(true);
    audioRequest.current?.abort();
    const controller = new AbortController();
    audioRequest.current = controller;
    const generation = tokenStore.generation();
    try {
      const query = new URLSearchParams(
        Object.fromEntries(
          Object.entries(transcript.audio_range).map(([key, value]) => [
            key,
            String(value),
          ]),
        ),
      );
      const response = await authenticatedFetch(
        `/sessions/${sessionId}/audio?${query}`,
        { signal: controller.signal },
      );
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(
          body?.detail?.message ?? "原音不可用，可能已到期或被隔离。",
        );
      }
      const blob = await response.blob();
      if (controller.signal.aborted || generation !== tokenStore.generation()) return;
      if (audioUrlRef.current) URL.revokeObjectURL(audioUrlRef.current);
      audioUrlRef.current = URL.createObjectURL(blob);
      setAudioUrl(audioUrlRef.current);
    } catch (error) {
      if (!isCancelled(error)) onError(error);
    } finally {
      if (audioRequest.current === controller && !controller.signal.aborted) setAudioLoading(false);
    }
  }
  return (
    <aside className="evidence-panel">
      <div className="evidence-heading">
        <h2>来源与事实</h2>
        <span className="count-badge">{facts.length}</span>
        <button className="text-button push" disabled={PUBLIC_DEMO || !sessionId || !extractionReady || busy || extracting || !transcripts.length} title={PUBLIC_DEMO ? "公开演示未连接模型服务" : extractionReady ? "提取当前稳定来源的临床事实" : "医院事实提取服务尚未配置"} onClick={() => void run(async () => {
          const result = await api<Job>(`/sessions/${sessionId}/extract-facts`, { method: "POST" });
          setExtractionJob(result);
          setTab("facts");
          await onRefresh();
        })}><Sparkles size={14} />提取事实</button>
      </div>
      {extractionJob && <div className="job-strip"><Sparkles size={14} /><span>事实提取</span><Status value={extractionJob.status} /></div>}
      <div className="mini-tabs" role="tablist" aria-label="来源类别">
        <button
          role="tab"
          aria-selected={tab === "transcripts"}
          onClick={() => setTab("transcripts")}
        >
          <MessageSquareText size={14} />
          转写 <span>{transcripts.length}</span>
        </button>
        <button
          role="tab"
          aria-selected={tab === "facts"}
          onClick={() => setTab("facts")}
        >
          <Link2 size={14} />
          事实 <span>{facts.length}</span>
        </button>
      </div>
      {tab === "transcripts" ? (
        <>
          <div className="evidence-toolbar">
            <span>当前会话 · {sessionId ? connection : "稳定片段"}</span>
            <button
              className="text-button"
              disabled={PUBLIC_DEMO || !sessionId}
              onClick={() => editTranscript()}
            >
              <Plus size={14} />
              医生补充
            </button>
          </div>
          <div className="evidence-scroll">
            {partials.map((item) => <article className="transcript-item partial-transcript" key={JSON.stringify([item.run_id, item.channel_id, item.capture_epoch, item.segment_id])}>
              <div className="transcript-meta"><span className="tag warn">识别中 · 暂未定稿</span><span>{item.channel_id === "patient_mic" ? "患者通道" : "医生通道"}</span></div>
              <p>{item.text}</p>
            </article>)}
            {transcripts.length ? (
              transcripts.map((item, index) => (
                <article className="transcript-item" key={item.id}>
                  <div className="transcript-meta">
                    <span className={`speaker-badge ${item.speaker}`}>
                      {ROLE_NAMES[item.speaker] ?? item.speaker}
                    </span>
                    <span>
                      陈述对象：{ROLE_NAMES[item.subject] ?? item.subject}
                    </span>
                    <span className="push">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                  </div>
                  <p>{item.text}</p>
                  <div className="transcript-footer">
                    <span>
                      {PUBLIC_DEMO ? "预置虚构脚本" : item.audio_range ? "音频来源" : "医生录入"} · v
                      {item.revision}
                    </span>
                    <div className="inline">
                      <IconButton
                        label="编辑转写与角色"
                        disabled={PUBLIC_DEMO}
                        onClick={() => editTranscript(item)}
                      >
                        <Pencil size={14} />
                      </IconButton>
                      {item.audio_range && (
                        <IconButton
                          label="回放原音"
                          disabled={PUBLIC_DEMO || audioLoading}
                          onClick={() => void playTranscript(item)}
                        >
                          <AudioLines size={15} />
                        </IconButton>
                      )}
                    </div>
                  </div>
                </article>
              ))
            ) : partials.length ? null : (
              <Empty
                icon={<MessageSquareText size={30} />}
                title="还没有转写"
                description={
                  sessionId
                    ? "录音稳定结果和医生补充将保存在本会话。"
                    : "新建录音会话后可录入。"
                }
              />
            )}
          </div>
          {audioLoading && <Loading text="读取原音…" />}
          {audioUrl && (
            <div className="audio-player">
              <audio controls autoPlay src={audioUrl} />
              <IconButton
                label="关闭回放"
                onClick={() => {
                  setAudioUrl("");
                  URL.revokeObjectURL(audioUrlRef.current);
                  audioUrlRef.current = "";
                }}
              >
                <X size={14} />
              </IconButton>
            </div>
          )}
        </>
      ) : (
        <>
          <div className="evidence-toolbar">
            <span>
              {
                facts.filter((item) => item.confirmation_status === "confirmed")
                  .length
              }{" "}
              项已确认
            </span>
            <span>保留来源版本</span>
          </div>
          <div className="evidence-scroll">
            {facts.length ? (
              facts.map((item) => (
                <button
                  className={`fact-item ${item.id === selectedFact?.id ? "selected" : ""}`}
                  key={item.id}
                  onClick={() => { factRequest.current?.abort(); setFactLoading(false); setResolutionReason(""); setFact({ ...item }); }}
                >
                  <div className="fact-meta">
                    <span>
                      {SECTION_NAMES[item.section] ??
                        item.section ??
                        "临床事实"}
                    </span>
                    {item.confirmation_status === "excluded" ? <span className="tag">已排除</span> : item.conflict_status &&
                    item.conflict_status !== "none" &&
                    item.conflict_status !== "resolved" ? (
                      <span className="tag warn">待核查</span>
                    ) : item.confirmation_status?.toLowerCase() ===
                      "confirmed" ? (
                      <Check size={14} className="teal" />
                    ) : (
                      <CircleHelp size={14} className="muted" />
                    )}
                  </div>
                  <p>{item.text}</p>
                  <div className="fact-meta">
                    <span>
                      {ROLE_NAMES[item.speaker]} → {ROLE_NAMES[item.subject]} ·
                      v{item.revision}
                    </span>
                    <ChevronRight size={14} />
                  </div>
                </button>
              ))
            ) : (
              <Empty
                icon={<BookOpen size={30} />}
                title="暂无临床事实"
                description="稳定来源处理完成后可核查。"
              />
            )}
          </div>
        </>
      )}
      {adding && (
        <Modal
          title={PUBLIC_DEMO ? "转写来源（虚构）" : editing ? "修订转写与角色" : "医生补充记录"}
          onClose={() => setAdding(false)}
        >
          <div className="form-stack">
            <div className="form-two">
              <label>
                陈述者
                <select
                  disabled={PUBLIC_DEMO}
                  value={speaker}
                  onChange={(event) => setSpeaker(event.target.value)}
                >
                  {["doctor", "patient", "family", "unknown"].map((role) => (
                    <option value={role} key={role}>
                      {ROLE_NAMES[role]}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                临床主体
                <select
                  disabled={PUBLIC_DEMO}
                  value={subject}
                  onChange={(event) => setSubject(event.target.value)}
                >
                  {["patient", "family", "other", "unknown"].map((role) => (
                    <option value={role} key={role}>
                      {ROLE_NAMES[role]}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {!editing && (
              <label>
                内容分类
                <select
                  value={section}
                  onChange={(event) => setSection(event.target.value)}
                >
                  {Object.entries(SECTION_NAMES).map(([key, value]) => (
                    <option value={key} key={key}>
                      {value}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label>
              原始陈述
              <textarea
                readOnly={PUBLIC_DEMO}
                rows={7}
                value={text}
                onChange={(event) => setText(event.target.value)}
                placeholder="输入已明确获取的临床信息"
              />
            </label>
            {editing && !PUBLIC_DEMO && <Notice>修订来源会使依赖此来源的文书审核失效。</Notice>}
            <div className="modal-actions">
              <button className="secondary" onClick={() => setAdding(false)}>
                取消
              </button>
              <button
                className="primary"
                disabled={PUBLIC_DEMO || busy || !text.trim()}
                onClick={() => void saveTranscript()}
              >
                <Save size={16} />
                保存{editing ? ` v${editing.revision + 1}` : "补充"}
              </button>
            </div>
          </div>
        </Modal>
      )}
      {factLoading && <Modal title="临床事实核查" onClose={() => { factRequest.current?.abort(); setFactLoading(false); }}><Loading text="读取来源事实…" /></Modal>}
      {fact && (
        <Modal title="临床事实核查" onClose={() => setFact(null)} wide>
          <div className="form-stack">
            <label>
              事实原文
              <textarea
                aria-label="事实原文"
                readOnly={PUBLIC_DEMO}
                value={fact.text}
                rows={3}
                onChange={(event) =>
                  setFact({ ...fact, text: event.target.value })
                }
              />
            </label>
            <div className="form-two">
              <label>
                陈述者
                <select
                  disabled={PUBLIC_DEMO}
                  value={fact.speaker}
                  onChange={(event) =>
                    setFact({ ...fact, speaker: event.target.value })
                  }
                >
                  {["doctor", "patient", "family", "unknown"].map((role) => (
                    <option value={role} key={role}>
                      {ROLE_NAMES[role]}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                临床主体
                <select
                  disabled={PUBLIC_DEMO}
                  value={fact.subject}
                  onChange={(event) =>
                    setFact({ ...fact, subject: event.target.value })
                  }
                >
                  {["patient", "family", "other", "unknown"].map((role) => (
                    <option value={role} key={role}>
                      {ROLE_NAMES[role]}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                肯否
                <select
                  disabled={PUBLIC_DEMO}
                  value={fact.polarity}
                  onChange={(event) =>
                    setFact({ ...fact, polarity: event.target.value })
                  }
                >
                  <option value="positive">肯定</option>
                  <option value="negative">否认</option>
                  <option value="unknown">不详</option>
                </select>
              </label>
              <label>
                确定性
                <select
                  disabled={PUBLIC_DEMO}
                  value={fact.certainty}
                  onChange={(event) =>
                    setFact({ ...fact, certainty: event.target.value })
                  }
                >
                  <option value="certain">明确</option>
                  <option value="uncertain">不确定</option>
                  <option value="suspected">疑似</option>
                  <option value="unknown">不详</option>
                </select>
              </label>
              <label>
                获取状态
                <select
                  disabled={PUBLIC_DEMO}
                  value={fact.elicitation}
                  onChange={(event) =>
                    setFact({ ...fact, elicitation: event.target.value })
                  }
                >
                  <option value="asked">已询问</option>
                  <option value="not_asked">未问</option>
                  <option value="not_applicable">临床不适用</option>
                  <option value="unverifiable">无法核实</option>
                  <option value="refused">拒答</option>
                </select>
              </label>
              <label>
                冲突状态
                <select
                  disabled={PUBLIC_DEMO}
                  value={fact.conflict_status}
                  onChange={(event) =>
                    setFact({ ...fact, conflict_status: event.target.value })
                  }
                >
                  <option value="none">无冲突</option>
                  <option value="unresolved">存在未解决冲突</option>
                  <option value="resolved">已核实处理</option>
                </select>
              </label>
            </div>
            <label>
              核查结论
              <select disabled={PUBLIC_DEMO} aria-label="核查结论" value={fact.confirmation_status} onChange={(event) => setFact({ ...fact, confirmation_status: event.target.value })}>
                <option value="unconfirmed">待核查</option>
                <option value="confirmed">已核查原始来源并确认</option>
                <option value="excluded">排除本项事实</option>
              </select>
            </label>
            <label>核查与处理说明{fact.confirmation_status === "excluded" ? "（排除必填）" : ""}<textarea readOnly={PUBLIC_DEMO} aria-label="核查与处理说明" rows={2} maxLength={2000} required={fact.confirmation_status === "excluded"} value={resolutionReason} onChange={event=>setResolutionReason(event.target.value)} placeholder="来源修订、排除事实或解决冲突时必填"/></label>
            <div className="source-links">
              <h3>证据引用</h3>
              {fact.source_ids?.map((id) => {
                const found = sources.find((s) => s.id === id);
                const transcript = transcripts.find((t) => t.id === id);
                return (
                  <button
                    className="source-link"
                    key={id}
                    onClick={() => {
                      if (found) setSource(found);
                      else if (transcript) {
                        setFact(null);
                        editTranscript(transcript);
                      } else
                        setSource({
                          id,
                          title: "来源引用",
                          content: { source_id: id, evidence: fact.evidence },
                        });
                    }}
                  >
                    <FileText size={15} />
                    <span>{found?.title ?? transcript?.text ?? id}</span>
                    <ChevronRight size={15} />
                  </button>
                );
              })}
              {!fact.source_ids?.length && (
                <p className="muted">未关联来源，不能当作已证实事实。</p>
              )}
              {fact.evidence?.length > 0 && (
                <details>
                  <summary>来源范围与版本</summary>
                  <StructuredData value={fact.evidence} />
                </details>
              )}
            </div>
            <div className="modal-actions">
              <span className="muted">基础版本 v{fact.revision}</span>
              <button
                className="primary"
                disabled={PUBLIC_DEMO || busy || !fact.text.trim() || (fact.confirmation_status === "excluded" && resolutionReason.trim().length < 3)}
                onClick={() => void saveFact()}
              >
                <Save size={16} />
                保存核查
              </button>
            </div>
          </div>
        </Modal>
      )}
      {source && (
        <Modal
          title={source.title ?? "来源详情"}
          onClose={() => setSource(null)}
          wide
        >
          <div className="form-stack">
            <StructuredData value={source.content ?? source.data ?? source} />
          </div>
        </Modal>
      )}
    </aside>
  );
}
