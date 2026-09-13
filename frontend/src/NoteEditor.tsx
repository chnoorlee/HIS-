import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  ArrowUpRight,
  Check,
  CheckCheck,
  ChevronDown,
  ChevronUp,
  ClipboardCheck,
  Download,
  FileClock,
  FilePlus2,
  FileText,
  History,
  Link2,
  LockKeyhole,
  LocateFixed,
  Plus,
  RefreshCw,
  Save,
  Sparkles,
} from "lucide-react";
import { ApiError, api, isCancelled, post, patch } from "./api";
import {
  Empty,
  IconButton,
  Modal,
  Notice,
  Status,
  dateText,
} from "./components";
import {
  NOTE_TYPES,
  ROLE_NAMES,
  type Block,
  type ExportOperation,
  type Fact,
  type Issue,
  type Job,
  type Note,
  type Review,
  type ReferenceReview,
} from "./types";
type PendingNoteAction = { kind: "select"; note: Note } | { kind: "create"; noteType: string };
export default function NoteEditor({
  encounterId,
  sessionId,
  patientName,
  notes,
  note,
  onSelect,
  onRefresh,
  onError,
  onFact,
  onDirty,
}: {
  encounterId: string;
  sessionId?: string;
  patientName: string;
  notes: Note[];
  note: Note | null;
  onSelect: (note: Note) => void;
  onRefresh: () => Promise<void>;
  onError: (error: unknown) => void;
  onFact: (id: string, sessionId: string) => void;
  onDirty: (dirty: boolean) => void;
}) {
  const [type, setType] = useState("admission");
  const [blocks, setBlocks] = useState<Block[]>([]);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [review, setReview] = useState<Review | null>(null);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [issues, setIssues] = useState<Issue[]>([]);
  const [resolutions, setResolutions] = useState<Record<string, string>>({});
  const [exportOp, setExportOp] = useState<ExportOperation | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyRevision, setHistoryRevision] = useState<number | null>(null);
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [sections, setSections] = useState<string[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [conflict, setConflict] = useState(false);
  const [activeSection, setActiveSection] = useState("");
  const [focusSection, setFocusSection] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<PendingNoteAction | null>(null);
  const [pendingError, setPendingError] = useState("");
  const [referenceBlock, setReferenceBlock] = useState<string | null>(null);
  const [referenceMode, setReferenceMode] = useState<"review" | "sources">("review");
  const [referenceSelection, setReferenceSelection] = useState<string[]>([]);
  const [referenceText, setReferenceText] = useState("");
  const [referenceFacts, setReferenceFacts] = useState<Fact[]>([]);
  const [referenceLoading, setReferenceLoading] = useState(false);
  const [referenceError, setReferenceError] = useState("");
  const [referenceReason, setReferenceReason] = useState("");
  const [referenceReviews, setReferenceReviews] = useState<Record<string, ReferenceReview>>({});
  const [referenceSaveError, setReferenceSaveError] = useState("");
  const documentRoot = useRef<HTMLDivElement>(null);
  const sectionNavigator = useRef<HTMLElement>(null);
  const editBase = useRef<{id?:string;revision?:number}>({});
  const dirtyRef = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => {
    if (dirtyRef.current && editBase.current.id === note?.id && editBase.current.revision !== note?.revision) {
      setConflict(true);
      return;
    }
    editBase.current = {id:note?.id,revision:note?.revision};
    dirtyRef.current = false;
    setBlocks(note?.blocks?.map((b) => ({ ...b })) ?? []);
    setType(note?.note_type ?? "admission");
    setDirty(false);
    onDirty(false);
    setReview(null);
    setReviewOpen(false);
    setResolutions({});
    setSuggestOpen(false);
    setIssues(note?.issues ?? []);
    setConflict(false);
    setExportOp(null);
    setActiveSection(note?.blocks?.[0]?.key ?? "");
    setFocusSection(null);
    setPendingAction(null);
    setPendingError("");
    setReferenceBlock(null);
    setReferenceReviews({});
    setReferenceSaveError("");
  }, [note?.id, note?.revision, onDirty]);
  useEffect(() => {
    if (!referenceBlock || !note) return;
    const controller = new AbortController();
    setReferenceLoading(true);
    setReferenceError("");
    setReferenceReason("");
    setReferenceFacts([]);
    void api<Fact[]>(`/sessions/${note.session_id}/facts`, { signal: controller.signal })
      .then((data) => { if (!controller.signal.aborted) setReferenceFacts(data); })
      .catch((error) => {
        if (controller.signal.aborted || isCancelled(error)) return;
        setReferenceError("无法读取当前引用事实，请关闭后重试。");
        onError(error);
      })
      .finally(() => { if (!controller.signal.aborted) setReferenceLoading(false); });
    return () => controller.abort();
  }, [referenceBlock, note?.id, note?.session_id, onError]);
  useLayoutEffect(() => {
    const root = documentRoot.current;
    const resize = () => root?.querySelectorAll("textarea").forEach((input) => {
        input.style.height = "auto";
        input.style.height = `${Math.max(76, input.scrollHeight)}px`;
      });
    resize();
    let width = root?.clientWidth;
    const observer = new ResizeObserver(() => {
      if (root?.clientWidth === width) return;
      width = root?.clientWidth;
      resize();
    });
    if (root) observer.observe(root);
    return () => observer.disconnect();
  }, [blocks]);
  useEffect(() => {
    let frame = 0;
    const update = () => {
      frame = 0;
      const targets = Array.from(documentRoot.current?.querySelectorAll<HTMLElement>("[data-block-key]") ?? []);
      const edge = (sectionNavigator.current?.getBoundingClientRect().bottom ?? 0) + 18;
      const focused = document.activeElement?.closest<HTMLElement>("[data-block-key]");
      const rect = focused?.getBoundingClientRect();
      const current = focused && rect && rect.bottom > edge && rect.top < window.innerHeight - 80
        ? focused : targets.filter((target) => target.getBoundingClientRect().top <= edge).at(-1) ?? targets[0];
      if (current) setActiveSection(current.dataset.blockKey ?? "");
    };
    const schedule = () => { if (!frame) frame = requestAnimationFrame(update); };
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    schedule();
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
    };
  }, [note?.id, blocks.length]);
  useEffect(() => {
    if (!focusSection || reviewOpen) return;
    const frame = requestAnimationFrame(() => {
      const target = Array.from(documentRoot.current?.querySelectorAll<HTMLElement>("[data-block-key]") ?? [])
        .find((section) => section.dataset.blockKey === focusSection);
      target?.scrollIntoView({ block: "start" });
      const input = target?.querySelector<HTMLTextAreaElement>("textarea:not(:disabled)");
      (input ?? target)?.focus({ preventScroll: true });
      setActiveSection(focusSection);
      setFocusSection(null);
    });
    return () => cancelAnimationFrame(frame);
  }, [focusSection, reviewOpen]);
  useEffect(() => {
    if (!dirty) {
      setIssues(note?.issues ?? []);
    }
  }, [note, dirty]);
  useEffect(() => {
    if (
      !job ||
      !["PENDING", "RUNNING", "QUEUED", "RETRY"].includes(
        job.status.toUpperCase(),
      )
    )
      return;
    let stopped = false;
    const timer = setInterval(() => {
      void api<Job>(`/jobs/${job.id}`)
        .then(async (current) => {
          if (stopped) return;
          setJob(current);
          if (
            ["SUCCEEDED", "COMPLETED", "FAILED"].includes(
              current.status.toUpperCase(),
            )
          ) {
            await onRefresh();
            if (current.status.toUpperCase() === "FAILED")
              onError(new Error(current.error ?? "章节建议任务失败"));
          }
        })
        .catch(onError);
    }, 1800);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [job, onRefresh, onError]);
  async function run<T,>(action: () => Promise<T>): Promise<T | undefined> {
    setBusy(true);
    try {
      return await action();
    } catch (error) {
      if (!mounted.current || isCancelled(error)) return;
      if (
        error instanceof ApiError &&
        error.status === 409 &&
        error.code === "revision_conflict"
      )
        setConflict(true);
      if (error instanceof ApiError && error.code === "fact_revision_conflict") {
        setReferenceSaveError("引用事实已再次更新，当前编辑仍保留。请重新核对引用后保存。");
        return;
      }
      onError(error);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  async function create(noteType = type) {
    if (!sessionId) return;
    await run(async () => {
      const created = await post<Note>("/notes", {
        session_id: sessionId,
        note_type: noteType,
      });
      if (!mounted.current) return;
      onSelect(created);
      await onRefresh();
    });
  }
  async function performAction(action: PendingNoteAction) {
    if (action.kind === "select") onSelect(action.note);
    else await create(action.noteType);
  }
  function requestAction(action: PendingNoteAction) {
    if (action.kind === "select" && action.note.id === note?.id) return;
    if (dirtyRef.current) {
      setPendingError("");
      setPendingAction(action);
    } else void performAction(action);
  }
  function updateBlock(index: number, text: string) {
    setBlocks((current) =>
      current.map((block, i) => (i === index ? { ...block, text } : block)),
    );
    setDirty(true);
    dirtyRef.current = true;
    onDirty(true);
    setReview(null);
  }
  async function save() {
    if (!note) return;
    return run(async () => {
      const saved = await patch<Note>(`/notes/${note.id}`, {
        base_revision: editBase.current.revision ?? note.revision,
        blocks: blocks.map(({ key, title, text, fact_ids }) => ({
          key,
          title,
          text,
          fact_ids,
          ...(referenceReviews[key] ? { reference_review: referenceReviews[key] } : {}),
        })),
      });
      if (!mounted.current) return;
      setDirty(false);
      dirtyRef.current = false;
      onDirty(false);
      onSelect(saved);
      await onRefresh().catch(onError);
      return saved;
    });
  }
  function locateSection(key: string) {
    setReviewOpen(false);
    setFocusSection(key);
  }
  function openReferences(block: Block, mode: "review" | "sources") {
    setReferenceMode(mode);
    setReferenceSelection([...block.fact_ids]);
    setReferenceText(block.text);
    setReferenceBlock(block.key);
  }
  async function generate() {
    if (!note) return;
    await run(async () => {
      const result = await post<Job>(`/notes/${note.id}/suggestions`, {
        base_revision: note.revision,
        sections,
      });
      if (!mounted.current) return;
      setJob(result);
      setSuggestOpen(false);
      await onRefresh();
    });
  }
  async function approve() {
    if (!note) return;
    setBusy(true);
    try {
      const result = await post<Review>(`/notes/${note.id}/review`, {
        base_revision: note.revision,
        issue_resolutions: Object.entries(resolutions)
          .filter(([, reason]) => reason.trim())
          .map(([issue_id, reason]) => ({
            issue_id,
            resolution: reason,
          })),
      });
      if (!mounted.current) return;
      setReview(result);
      setReviewOpen(false);
      await onRefresh();
    } catch (error) {
      if (!mounted.current || isCancelled(error)) return;
      if (error instanceof ApiError && error.code === "review_blocked") {
        setIssues((error.details?.issues ?? []) as Issue[]);
      } else onError(error);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  async function exportNote() {
    if (!note) return;
    await run(async () => {
      const result = await post<ExportOperation>(`/notes/${note.id}/exports`, {
        review_id: review?.id ?? note.review_id,
        idempotency_key: crypto.randomUUID(),
      });
      if (!mounted.current) return;
      setExportOp(result);
      await onRefresh();
    });
  }
  async function reconcile() {
    if (!exportOp) return;
    await run(async () => {
      setExportOp(
        await post<ExportOperation>(`/exports/${exportOp.id}/reconcile`),
      );
    });
  }
  function download() {
    if (!note) return;
    const text = `${patientName} - ${NOTE_TYPES[note.note_type]}\n版本 ${note.revision} | ${note.id}\n状态 ${note.status}\n\n${blocks.map((b) => `${b.title}\n${b.text}`).join("\n\n")}`;
    const url = URL.createObjectURL(
      new Blob([text], { type: "text/plain;charset=utf-8" }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `病历草稿_${note.id}_v${note.revision}.txt`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  const history = note?.revisions?.find((r) => r.revision === historyRevision);
  const reviewed = Boolean(
    (review?.note_revision === note?.revision || note?.review_id) &&
    note?.status === "REVIEWED" &&
    !dirty,
  );
  const hasRunningJob =
    job && ["PENDING", "RUNNING", "QUEUED"].includes(job.status.toUpperCase());
  const activeIndex = Math.max(0, blocks.findIndex((block) => block.key === activeSection));
  const filledSections = blocks.filter((block) => block.text.trim()).length;
  const reviewingBlock = blocks.find((block) => block.key === referenceBlock);
  const allReferencesAvailable = referenceSelection.every((id) => referenceFacts.some((fact) => fact.id === id && fact.confirmation_status !== "excluded"));
  return (
    <section className="note-workspace">
      <div className="note-toolbar">
        <div className="inline gap">
          <FileText size={17} className="teal" />
          <h2>病历文书</h2>
          <span className="count-badge">{notes.length}</span>
        </div>
        <div className="inline gap">
          <select
            aria-label="文书记录"
            disabled={busy}
            value={note?.id ?? ""}
            onChange={(event) => {
              const found = notes.find((n) => n.id === event.target.value);
              if (found) requestAction({ kind: "select", note: found });
            }}
          >
            <option value="" disabled>
              选择文书
            </option>
            {notes.map((item) => (
              <option value={item.id} key={item.id}>
                {NOTE_TYPES[item.note_type]} · v{item.revision} ·{" "}
                {dateText(item.created_at)}
              </option>
            ))}
          </select>
          <IconButton
            label="文书版本记录"
            disabled={!note}
            onClick={() => setHistoryOpen(true)}
          >
            <History size={17} />
          </IconButton>
        </div>
      </div>
      <div className="note-types" role="tablist" aria-label="文书类型">
        {Object.entries(NOTE_TYPES).map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={type === key}
            disabled={busy}
            onClick={() => {
              const found = notes.find((n) => n.note_type === key);
              if (found) requestAction({ kind: "select", note: found });
              else setType(key);
            }}
          >
            {label}
          </button>
        ))}
        <IconButton
          label={`新建${NOTE_TYPES[type]}`}
          disabled={!sessionId || busy}
          onClick={() => requestAction({ kind: "create", noteType: type })}
        >
          <Plus size={16} />
        </IconButton>
      </div>
      {conflict && (
        <Notice kind="bad">
          记录已被更新，当前编辑仍保留。
          <button
            className="text-button"
            onClick={() =>
              void run(async () => {
                if (!window.confirm("重新载入将放弃当前未保存修改，是否继续？"))
                  return;
                if (!note) return;
                const current = await api<Note>(`/notes/${note.id}`);
                if (!mounted.current) return;
                setBlocks(current.blocks.map(block=>({...block})));
                editBase.current = {id:current.id,revision:current.revision};
                dirtyRef.current = false;
                setDirty(false);
                onDirty(false);
                setReferenceReviews({});
                setReferenceSaveError("");
                setReferenceBlock(null);
                onSelect(current);
                setConflict(false);
                await onRefresh();
              })
            }
          >
            重新载入服务端版本
          </button>
        </Notice>
      )}
      {referenceSaveError && <Notice kind="bad">{referenceSaveError}</Notice>}
      {job && (
        <div className="job-strip">
          <Sparkles size={15} />
          <span>章节整理任务</span>
          <Status value={job.status} />
          <span className="push muted">{job.id.slice(-8)}</span>
        </div>
      )}
      {!note ? (
        <Empty
          icon={<FilePlus2 size={42} />}
          title={`尚未建立${NOTE_TYPES[type]}`}
          description={
            sessionId
              ? "文书保留来源、医生修改和每次审核版本。"
              : "先新建患者录音会话。"
          }
          action={
            <button
              className="primary"
              disabled={!sessionId || busy}
              onClick={() => requestAction({ kind: "create", noteType: type })}
            >
              <Plus size={16} />
              新建{NOTE_TYPES[type]}
            </button>
          }
        />
      ) : (
        <>
          <div className="document-meta">
            <div>
              <h1>{NOTE_TYPES[note.note_type]}</h1>
              <span>
                {patientName} · {dateText(note.created_at)} · 文书{" "}
                {note.id.slice(-8)}
              </span>
            </div>
            <div className="meta-status">
              <Status value={note.status} />
              <span className="version-chip">v{note.revision}</span>
            </div>
          </div>
          {blocks.length > 0 && (
            <nav className="section-navigator" aria-label="文书章节定位" ref={sectionNavigator}>
              <label>
                <span>章节</span>
                <select aria-label="定位文书章节" value={blocks[activeIndex]?.key ?? ""} onChange={(event) => locateSection(event.target.value)}>
                  {blocks.map((block, index) => <option key={block.key} value={block.key}>{String(index + 1).padStart(2, "0")} {block.title}{!block.text.trim() ? " · 待补充" : ""}</option>)}
                </select>
              </label>
              <span className="section-progress">{filledSections}/{blocks.length} 已填写</span>
              <div className="inline gap">
                <IconButton label="上一章节" disabled={activeIndex === 0} onClick={() => locateSection(blocks[activeIndex - 1].key)}><ChevronUp size={18} /></IconButton>
                <IconButton label="下一章节" disabled={activeIndex === blocks.length - 1} onClick={() => locateSection(blocks[activeIndex + 1].key)}><ChevronDown size={18} /></IconButton>
              </div>
            </nav>
          )}
          <div className="document-scroll" ref={documentRoot}>
            {blocks.map((block, index) => (
              <section className={`document-block ${activeSection === block.key ? "current-section" : ""}`} key={block.key} data-block-key={block.key} tabIndex={-1}>
                <div className="block-heading">
                  <h3>
                    <span className="block-number">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    {block.title}
                  </h3>
                  <div className="inline gap">
                    {issues.some((issue) => issue.block_key === block.key || (issue.fact_id && block.fact_ids.includes(issue.fact_id))) && <span className="tag warn">待核查</span>}
                    {block.fact_ids.length > 0 && (referenceReviews[block.key] || issues.some((issue) => issue.code === "fact_changed" && issue.block_key === block.key)) && <button className="text-button" disabled={busy || note.status === "QUARANTINED"} onClick={() => openReferences(block, "review")}><ClipboardCheck size={14} />{referenceReviews[block.key] ? "引用已核对 · 待保存" : "核对引用"}</button>}
                    <IconButton label={`章节来源 · ${block.title}`} disabled={busy || note.status === "QUARANTINED"} onClick={() => openReferences(block, "sources")}><Link2 size={14} /></IconButton>
                    {block.protected && (
                      <span className="protected-label">
                        <LockKeyhole size={11} />
                        医生编辑
                      </span>
                    )}
                    {!block.text?.trim() && (
                      <span className="tag warn">待补充</span>
                    )}
                  </div>
                </div>
                <textarea
                  aria-label={block.title}
                  value={block.text ?? ""}
                  placeholder={`填写${block.title}，保留未问、不详与不确定信息`}
                  onChange={(event) => updateBlock(index, event.target.value)}
                  onFocus={() => setActiveSection(block.key)}
                  rows={Math.max(
                    2,
                    Math.min(9, Math.ceil((block.text?.length ?? 0) / 60)),
                  )}
                  disabled={busy || note.status === "QUARANTINED"}
                />
                {block.fact_ids?.length > 0 && (
                  <div className="block-evidence">
                    <Link2 size={12} />
                    <span>支持来源</span>
                    {block.fact_ids.map((id, i) => (
                      <button
                        key={id}
                        title={`核查事实 ${id}`}
                        onClick={() => onFact(id, note.session_id)}
                      >
                        {i + 1}
                      </button>
                    ))}
                  </div>
                )}
              </section>
            ))}
            {!blocks.length && (
              <Empty
                icon={<FileText size={30} />}
                title="当前文书没有章节"
                description="请检查文书模板配置。"
              />
            )}
          </div>
          <div className="note-actionbar">
            <span className={`save-state ${dirty ? "unsaved" : ""}`}>
              {dirty ? (
                <>
                  <span className="connection-dot amber" />
                  未保存修改
                </>
              ) : (
                <>
                  <Check size={13} />
                  版本 v{note.revision} 已保存
                </>
              )}
            </span>
            <div className="inline gap">
              <IconButton
                label="下载当前文书文本"
                disabled={dirty || busy}
                onClick={download}
              >
                <Download size={17} />
              </IconButton>
              <button
                className="secondary"
                disabled={
                  busy ||
                  dirty ||
                  Boolean(hasRunningJob) ||
                  note.status === "QUARANTINED"
                }
                onClick={() => {
                  setSections(
                    blocks.filter((b) => !b.protected).map((b) => b.key),
                  );
                  setSuggestOpen(true);
                }}
              >
                <Sparkles size={15} />
                整理章节
              </button>
              <button
                className="secondary"
                disabled={busy || !dirty || note.status === "QUARANTINED"}
                onClick={() => void save()}
              >
                <Save size={15} />
                保存
              </button>
              <button
                className="primary"
                disabled={busy || dirty || note.status === "QUARANTINED"}
                onClick={() => {
                  setIssues(note.issues ?? []);
                  setReviewOpen(true);
                }}
              >
                <ClipboardCheck size={15} />
                审核
              </button>
            </div>
          </div>
          <div className="export-strip">
            <div className="inline gap">
              <span className={`review-icon ${reviewed ? "approved" : ""}`}>
                <CheckCheck size={18} />
              </span>
              <div>
                <strong>
                  {reviewed
                    ? `v${note.revision} 已完成医生审核`
                    : "写回前需审核当前版本"}
                </strong>
                <span>写入 EMR 草稿 · 院内系统完成正式签署</span>
              </div>
            </div>
            <button
              className="secondary"
              disabled={!reviewed || busy || exportOp?.status === "UNKNOWN"}
              onClick={() => void exportNote()}
            >
              <ArrowUpRight size={15} />
              写入 EMR
            </button>
          </div>
          {exportOp && (
            <div className="export-result">
              <div className="inline gap">
                <strong>写回记录</strong>
                <Status value={exportOp.status} />
                <span className="muted">{exportOp.id.slice(-8)}</span>
              </div>
              {exportOp.status === "UNKNOWN" && (
                <Notice>
                  远端结果未知，需先回读核实。
                  <button
                    className="text-button"
                    disabled={busy}
                    onClick={() => void reconcile()}
                  >
                    <RefreshCw size={14} />
                    回读核实
                  </button>
                </Notice>
              )}
              {exportOp.error && <p className="error-text">{exportOp.error}</p>}
              {(exportOp.external_id || exportOp.remote_document_id) && (
                <p className="muted">
                  EMR 文书：
                  {exportOp.external_id ?? exportOp.remote_document_id}
                </p>
              )}
            </div>
          )}
        </>
      )}
      {reviewOpen && note && (
        <Modal
          title={`审核 ${NOTE_TYPES[note.note_type]} · v${note.revision}`}
          onClose={() => setReviewOpen(false)}
          wide
        >
          <div className="form-stack">
            <div className="review-summary">
              <ClipboardCheck size={26} />
              <div>
                <strong>{patientName}</strong>
                <span>
                  精确版本 v{note.revision} · {note.id}
                </span>
              </div>
            </div>
            {issues.length ? (
              <div className="review-issues">
                {issues.map((issue) => {
                  const target = blocks.find((block) => block.key === issue.block_key)
                    ?? blocks.find((block) => issue.fact_id && block.fact_ids.includes(issue.fact_id));
                  return (
                  <div
                    className={`review-issue ${issue.severity}`}
                    key={issue.id}
                  >
                    <div className="inline gap issue-heading">
                      <span
                        className={`tag ${issue.severity === "blocker" ? "bad" : "warn"}`}
                      >
                        {issue.severity === "blocker" ? "阻止写回" : "需核查"}
                      </span>
                      <strong>{issue.message}</strong>
                      {target ? <button className="text-button issue-location" onClick={() => locateSection(target.key)}><LocateFixed size={15} />{target.title}</button>
                        : issue.fact_id ? <button className="text-button issue-location" onClick={() => { setReviewOpen(false); onFact(issue.fact_id!, note.session_id); }}><Link2 size={15} />核查来源</button> : null}
                    </div>
                    {issue.resolvable ? (
                      <label>
                        处理说明
                        <textarea
                          value={resolutions[issue.id] ?? ""}
                          onChange={(event) =>
                            setResolutions({
                              ...resolutions,
                              [issue.id]: event.target.value,
                            })
                          }
                          placeholder="填写核查过程与处理理由"
                          rows={2}
                        />
                      </label>
                    ) : (
                      <p className="muted">
                        需修订相应事实或补全文书后重新审核。
                      </p>
                    )}
                  </div>
                ); })}
              </div>
            ) : (
              <Notice kind="info">
                当前已加载版本无已知规则缺项。确认时服务端会重新校验内容、事实与来源版本。
              </Notice>
            )}
            <Notice kind="warn">
              确认表示已核查临床主体、否定与不确定信息、用药及原始来源。后续修订须重新审核。
            </Notice>
            <div className="modal-actions">
              <button
                className="secondary"
                onClick={() => setReviewOpen(false)}
              >
                返回编辑
              </button>
              <button
                className="primary"
                disabled={busy}
                onClick={() => void approve()}
              >
                <CheckCheck size={16} />
                确认审核 v{note.revision}
              </button>
            </div>
          </div>
        </Modal>
      )}
      {pendingAction && (
        <Modal title="保存当前文书修改" onClose={() => { if (!busy) setPendingAction(null); }}>
          <div className="form-stack">
            <p>{patientName} · {NOTE_TYPES[note?.note_type ?? type]} · 未保存修改</p>
            {pendingError && <Notice kind="bad">{pendingError}</Notice>}
            <div className="modal-actions unsaved-actions">
              <button className="secondary" disabled={busy} onClick={() => setPendingAction(null)}>继续编辑</button>
              <button className="secondary" disabled={busy} onClick={() => { const action = pendingAction; setPendingAction(null); void performAction(action); }}>放弃修改</button>
              <button className="primary" disabled={busy || conflict || note?.status === "QUARANTINED"} onClick={() => void (async () => {
                const action = pendingAction;
                const saved = await save();
                if (!mounted.current) return;
                if (!saved) { setPendingError("保存未成功，当前修改仍保留。"); return; }
                setPendingAction(null);
                await performAction(action);
              })()}><Save size={15} />保存并继续</button>
            </div>
          </div>
        </Modal>
      )}
      {reviewingBlock && (
        <Modal title={`${referenceMode === "review" ? "核对引用" : "章节来源"} · ${reviewingBlock.title}`} onClose={() => setReferenceBlock(null)} wide>
          <div className="form-stack">
            {referenceMode === "sources" ? <label>章节文字<textarea aria-label="章节文字" rows={5} maxLength={40000} value={referenceText} onChange={(event) => setReferenceText(event.target.value)} /></label>
              : <div className="reference-original"><h3>文书中的既有表述</h3><p>{reviewingBlock.text || "（空）"}</p></div>}
            {referenceLoading ? <p role="status">正在读取当前引用事实…</p> : referenceError ? <Notice kind="bad">{referenceError}</Notice> : (referenceMode === "review" ? reviewingBlock.fact_ids : [...new Set([...reviewingBlock.fact_ids, ...referenceFacts.map((fact) => fact.id)])]).map((id) => {
              const current = referenceFacts.find((fact) => fact.id === id);
              return <div className="reference-fact" key={id}>
                <div className="inline gap">
                  {referenceMode === "sources" ? <label className="checkbox-row"><input type="checkbox" aria-label={`引用 ${current?.text ?? id}`} checked={referenceSelection.includes(id)} disabled={(!current || current.confirmation_status === "excluded") && !referenceSelection.includes(id)} onChange={(event) => setReferenceSelection((selected) => event.target.checked ? [...selected, id] : selected.filter((value) => value !== id))} />引用事实</label> : <strong>引用事实</strong>}
                  <span className="muted">{reviewingBlock.fact_ids.includes(id) ? `原引用 v${reviewingBlock.facts_snapshot?.[id] ?? "?"} → ` : ""}当前 v{current?.revision ?? "?"}</span>
                </div>
                <p>{current?.text ?? "当前事实不可用"}</p>
                {current && <div className="inline gap reference-fact-actions"><span className="tag">{current.confirmation_status === "excluded" ? "已排除" : current.confirmation_status === "confirmed" ? "已确认" : "待确认"}</span><span className="muted">{ROLE_NAMES[current.subject] ?? current.subject} · {blocks.find((block) => block.key === current.section)?.title ?? current.section}</span>
                  {referenceMode === "sources" && <button className="text-button push" disabled={current.confirmation_status === "excluded"} onClick={() => {
                    setReferenceText((value) => `${value}${value.trim() ? "\n" : ""}${current.text}`);
                    setReferenceSelection((selected) => selected.includes(id) ? selected : [...selected, id]);
                  }}><Plus size={14} />插入原文</button>}
                </div>}
              </div>;
            })}
            {!referenceLoading && referenceMode === "sources" && !referenceFacts.length && <p className="muted">当前会话暂无事实。</p>}
            {referenceSelection.length > 0 && <label>引用核对说明<textarea rows={3} maxLength={2000} value={referenceReason} onChange={(event) => setReferenceReason(event.target.value)} /></label>}
            <div className="modal-actions">
              <button className="secondary" onClick={() => setReferenceBlock(null)}>取消</button>
              <button className="primary" disabled={referenceLoading || Boolean(referenceError) || !allReferencesAvailable || (referenceSelection.length > 0 && referenceReason.trim().length < 3) || referenceText.length > 40000} onClick={() => {
                const fact_revisions = Object.fromEntries(referenceSelection.map((id) => [id, referenceFacts.find((fact) => fact.id === id)!.revision]));
                setReferenceReviews((current) => {
                  const next = { ...current };
                  if (referenceSelection.length) next[reviewingBlock.key] = { fact_revisions, reason: referenceReason.trim() };
                  else delete next[reviewingBlock.key];
                  return next;
                });
                if (referenceMode === "sources") setBlocks((current) => current.map((block) => block.key === reviewingBlock.key ? { ...block, text: referenceText, fact_ids: referenceSelection } : block));
                setDirty(true);
                dirtyRef.current = true;
                onDirty(true);
                setReview(null);
                setReferenceSaveError("");
                setReferenceBlock(null);
              }}><ClipboardCheck size={16} />{referenceMode === "review" ? "确认本章引用" : "应用章节来源"}</button>
            </div>
          </div>
        </Modal>
      )}
      {suggestOpen && note && (
        <Modal title="整理文书章节" onClose={() => setSuggestOpen(false)}>
          <div className="form-stack">
            <Notice kind="info">
              来源快照与文书版本 v{note.revision}{" "}
              将固定保存。已受保护的医生修改保留。
            </Notice>
            {blocks.map((block) => (
              <label className="checkbox-row" key={block.key}>
                <input
                  type="checkbox"
                  disabled={block.protected}
                  checked={sections.includes(block.key)}
                  onChange={(event) =>
                    setSections(
                      event.target.checked
                        ? [...sections, block.key]
                        : sections.filter((s) => s !== block.key),
                    )
                  }
                />
                <span>{block.title}</span>
                {block.protected && <span className="tag">医生编辑</span>}
              </label>
            ))}
            <div className="modal-actions">
              <button
                className="secondary"
                onClick={() => setSuggestOpen(false)}
              >
                取消
              </button>
              <button
                className="primary"
                disabled={busy || !sections.length}
                onClick={() => void generate()}
              >
                <Sparkles size={15} />
                生成章节建议
              </button>
            </div>
          </div>
        </Modal>
      )}
      {historyOpen && note && (
        <Modal
          title="不可变文书版本"
          onClose={() => setHistoryOpen(false)}
          wide
        >
          <div className="form-stack">
            <div className="version-list">
              {note.revisions?.length ? (
                note.revisions.map((item) => (
                  <button
                    className={`version-row ${historyRevision === item.revision ? "selected" : ""}`}
                    key={item.revision}
                    onClick={() => setHistoryRevision(item.revision)}
                  >
                    <FileClock size={18} />
                    <strong>v{item.revision}</strong>
                    <span>{dateText(item.created_at)}</span>
                    {item.revision === note.revision && (
                      <span className="tag good">当前</span>
                    )}
                  </button>
                ))
              ) : (
                <div className="version-row">
                  <FileClock size={18} />
                  <strong>当前 v{note.revision}</strong>
                </div>
              )}
            </div>
            {history?.blocks?.map((block) => (
              <div className="history-block" key={block.key}>
                <h3>{block.title}</h3>
                <p>{block.text || "（空）"}</p>
              </div>
            ))}
            <span className="muted">
              历史版本仅供核查，继续编辑将创建新版本。
            </span>
          </div>
        </Modal>
      )}
    </section>
  );
}
