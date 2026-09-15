import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  Activity,
  AlertCircle,
  ArrowRight,
  BedDouble,
  Building2,
  CalendarDays,
  ChevronRight,
  ClipboardList,
  FileText,
  HeartPulse,
  Hospital,
  ListChecks,
  Loader2,
  LogOut,
  Menu,
  PanelLeftClose,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  Users,
  WifiOff,
  X,
} from "lucide-react";
import { ApiError, api, errorMessage, isCancelled, patch, post, tokenStore } from "./api";
import { clearIdentity, restoreIdentity, signIn, signOut, type AuthConfig } from "./auth";
import {
  Empty,
  IconButton,
  Loading,
  Modal,
  Notice,
  Status,
  StructuredData,
  dateText,
} from "./components";
import Recording from "./Recording";
import Evidence from "./Evidence";
import NoteEditor from "./NoteEditor";
import { bridgeCall, hasNativeBridge } from "./bridge";
import { PUBLIC_DEMO } from "./mode";
import {
  type AuditEntry,
  type Encounter,
  type ExportOperation,
  type Fact,
  type Job,
  type Note,
  type Session,
  type Source,
  type SystemStatus,
  type Transcript,
  type User,
} from "./types";
const NAV_ITEMS = [
  { id: "workspace", label: "病历工作台", icon: ClipboardList },
  { id: "sources", label: "院内资料", icon: Building2 },
  { id: "activity", label: "处理任务", icon: Activity },
  { id: "audit", label: "审计记录", icon: ShieldCheck },
  { id: "settings", label: "系统设置", icon: Settings2 },
];
export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [initializing, setInitializing] = useState(true);
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [system, setSystem] = useState<SystemStatus | null>(null);
  const [page, setPage] = useState("workspace");
  const [encounters, setEncounters] = useState<Encounter[]>([]);
  const [encounter, setEncounter] = useState<Encounter | null>(null);
  const [search, setSearch] = useState("");
  const [listLoading, setListLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [session, setSession] = useState<Session | null>(null);
  const [notes, setNotes] = useState<Note[]>([]);
  const [note, setNote] = useState<Note | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [transcripts, setTranscripts] = useState<Transcript[]>([]);
  const [facts, setFacts] = useState<Fact[]>([]);
  const [error, setError] = useState("");
  const errorToast = useRef<HTMLDivElement>(null);
  const topbar = useRef<HTMLElement>(null);
  const [online, setOnline] = useState(navigator.onLine);
  const [active, setActive] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [sidebar, setSidebar] = useState(false);
  const [selectedFact, setSelectedFact] = useState<{ id: string; sessionId: string }>();
  const [source, setSource] = useState<Source | null>(null);
  const encounterRef = useRef<string | undefined>(undefined);
  const sessionRef = useRef<string | undefined>(undefined);
  const noteRef = useRef<string | undefined>(undefined);
  const refreshSequence = useRef(0);
  const mounted = useRef(true);
  const activeRef = useRef(false);
  const startup = useRef<Promise<{ config: AuthConfig; identity: User | null }> | null>(null);
  const resetWorkspace = useCallback(() => {
    encounterRef.current = undefined;
    sessionRef.current = undefined;
    noteRef.current = undefined;
    activeRef.current = false;
    setUser(null);
    setSystem(null);
    setEncounter(null);
    setEncounters([]);
    setSessions([]);
    setSession(null);
    setNotes([]);
    setNote(null);
    setSources([]);
    setTranscripts([]);
    setFacts([]);
    setSource(null);
    setSelectedFact(undefined);
    setActive(false);
    setDirty(false);
    setSearch("");
    setPage("workspace");
    setSidebar(false);
    setLoading(false);
    setListLoading(false);
  }, []);
  const handleError = useCallback((value: unknown) => {
    if (isCancelled(value)) return;
    if (value instanceof ApiError && value.status === 401) {
      void clearIdentity();
      resetWorkspace();
      setError("登录已失效，请重新登录。");
    } else setError(errorMessage(value));
  }, [resetWorkspace]);
  const setCaptureActive = useCallback(
    (value: boolean) => { activeRef.current = value; setActive(value); },
    [],
  );
  const setNoteDirty = useCallback((value: boolean) => setDirty(value), []);
  useLayoutEffect(() => {
    const notice = errorToast.current;
    const shell = notice?.closest<HTMLElement>(".app-shell");
    if (!notice || !shell) return;
    const measure = () => shell.style.setProperty("--error-toast-height", `${notice.getBoundingClientRect().height}px`);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(notice);
    return () => { observer.disconnect(); shell.style.removeProperty("--error-toast-height"); };
  }, [error]);
  useLayoutEffect(() => {
    if (!PUBLIC_DEMO || !sidebar) return;
    const header = topbar.current;
    const shell = header?.closest<HTMLElement>(".app-shell");
    if (!header || !shell) return;
    const measure = () => shell.style.setProperty(
      "--public-demo-drawer-top",
      `${Math.max(0, header.getBoundingClientRect().bottom)}px`,
    );
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(header);
    const banner = shell.querySelector(".public-demo-banner");
    if (banner) observer.observe(banner);
    window.addEventListener("resize", measure);
    window.addEventListener("scroll", measure, { passive: true });
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
      window.removeEventListener("scroll", measure);
      shell.style.removeProperty("--public-demo-drawer-top");
    };
  }, [sidebar]);
  useEffect(() => {
    mounted.current = true;
    let cancelled = false;
    startup.current ??= (async () => {
      const config = await api<AuthConfig>("/auth/config");
      setAuthConfig(config);
      return { config, identity: await restoreIdentity(config) };
    })();
    void (async () => {
      try {
        const result = await startup.current!;
        if (!cancelled) { setAuthConfig(result.config); setUser(result.identity); }
      } catch (failure) {
        if (!cancelled) { void clearIdentity(); setError(errorMessage(failure)); }
      } finally {
        if (!cancelled) setInitializing(false);
      }
    })();
    const expired = () => {
      resetWorkspace();
      void clearIdentity();
      setError("登录已失效，请重新登录。");
    };
    window.addEventListener("his-auth-expired", expired);
    const status = () => setOnline(navigator.onLine);
    window.addEventListener("online", status);
    window.addEventListener("offline", status);
    return () => {
      mounted.current = false;
      cancelled = true;
      window.removeEventListener("his-auth-expired", expired);
      window.removeEventListener("online", status);
      window.removeEventListener("offline", status);
    };
  }, [resetWorkspace]);
  useEffect(() => {
    if (!user) return;
    void api<SystemStatus>("/system/status").then(setSystem).catch(handleError);
  }, [user, handleError]);
  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    setListLoading(true);
    const timer = setTimeout(() => {
      void api<Encounter[]>(`/encounters?search=${encodeURIComponent(search)}`)
        .then((data) => {
          if (cancelled) return;
          setEncounters(data);
          if (!encounterRef.current && data.length) {
            setEncounter(data[0]);
            encounterRef.current = data[0].id;
          }
        })
        .catch(handleError)
        .finally(() => {
          if (!cancelled) setListLoading(false);
        });
    }, 220);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [user, search, handleError]);
  const refresh = useCallback(async () => {
    const id = encounterRef.current;
    const generation = tokenStore.generation();
    if (!id) return;
    const sequence = ++refreshSequence.current;
    const [sessionList, noteList, sourceList] = await Promise.all([
      api<Session[]>(`/sessions?encounter_id=${encodeURIComponent(id)}`),
      api<Note[]>(`/notes?encounter_id=${encodeURIComponent(id)}`),
      api<Source[]>(`/encounters/${id}/sources`),
    ]);
    if (encounterRef.current !== id || generation !== tokenStore.generation() || sequence !== refreshSequence.current) return;
    setSessions(sessionList);
    setNotes(noteList);
    setSources(sourceList);
    const currentSession =
      sessionList.find((item) => item.id === sessionRef.current) ??
      sessionList[0] ??
      null;
    sessionRef.current = currentSession?.id;
    setSession(currentSession);
    const currentNote =
      noteList.find((item) => item.id === noteRef.current) ??
      noteList[0] ??
      null;
    noteRef.current = currentNote?.id;
    if (currentNote) {
      const details = await api<Note>(`/notes/${currentNote.id}`);
      if (encounterRef.current === id && noteRef.current === details.id && generation === tokenStore.generation() && sequence === refreshSequence.current)
        setNote((current) => current?.id === details.id && current.revision > details.revision ? current : details);
    } else setNote(null);
    if (currentSession) {
      const [ts, fs] = await Promise.all([
        api<Transcript[]>(`/sessions/${currentSession.id}/transcripts`),
        api<Fact[]>(`/sessions/${currentSession.id}/facts`),
      ]);
      if (
        encounterRef.current === id &&
        sessionRef.current === currentSession.id && generation === tokenStore.generation() && sequence === refreshSequence.current
      ) {
        setTranscripts(ts);
        setFacts(fs);
      }
    } else {
      setTranscripts([]);
      setFacts([]);
    }
  }, []);
  useEffect(() => {
    if (!encounter) return;
    setLoading(true);
    sessionRef.current = undefined;
    noteRef.current = undefined;
    setSession(null);
    setSessions([]);
    setNotes([]);
    setSources([]);
    setSource(null);
    setNote(null);
    setTranscripts([]);
    setFacts([]);
    setSelectedFact(undefined);
    let cancelled = false;
    void refresh()
      .catch(handleError)
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [encounter?.id, refresh, handleError]);
  useEffect(() => {
    if (PUBLIC_DEMO || !session?.id) return;
    let cancelled = false;
    const id = session.id;
    const timer = setInterval(() => {
      if (!navigator.onLine || document.visibilityState === "hidden") return;
      void Promise.all([
        api<Transcript[]>(`/sessions/${id}/transcripts`),
        api<Fact[]>(`/sessions/${id}/facts`),
        api<Session>(`/sessions/${id}`),
      ])
        .then(([ts, fs, current]) => {
          if (cancelled || sessionRef.current !== id) return;
          setTranscripts(ts);
          setFacts(fs);
          setSession(current);
        })
        .catch(handleError);
    }, 4000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [session?.id, handleError]);
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => {
      if (dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [dirty]);
  const selectSession = useCallback(
    (value: Session) => {
      if (value.encounter_id !== encounterRef.current) return;
      sessionRef.current = value.id;
      setSessions((current) => [value, ...current.filter((item) => item.id !== value.id)]);
      setSession(value);
      setTranscripts([]);
      setFacts([]);
      setSelectedFact(undefined);
      void refresh().catch(handleError);
    },
    [refresh, handleError],
  );
  const selectNote = useCallback(
    (value: Note) => {
      if (value.encounter_id !== encounterRef.current) return;
      noteRef.current = value.id;
      setNotes((current) => [value, ...current.filter((item) => item.id !== value.id)]);
      setNote(value);
      void api<Note>(`/notes/${value.id}`)
        .then((data) => {
          if (noteRef.current === data.id && encounterRef.current === data.encounter_id)
            setNote((current) => current?.id === data.id && current.revision > data.revision ? current : data);
        })
        .catch(handleError);
    },
    [handleError],
  );
  function selectEncounter(value: Encounter) {
    if (value.id === encounter?.id) {
      setSidebar(false);
      return;
    }
    if (activeRef.current) {
      setError("录音会话已固定当前就诊，请先结束录音再切换患者。");
      return;
    }
    if (dirty && !window.confirm("当前文书有未保存修改。放弃修改并切换患者？"))
      return;
    encounterRef.current = value.id;
    sessionRef.current = undefined;
    noteRef.current = undefined;
    setSessions([]);
    setSession(null);
    setNotes([]);
    setNote(null);
    setSources([]);
    setSource(null);
    setTranscripts([]);
    setFacts([]);
    setEncounter(value);
    setDirty(false);
    setSidebar(false);
  }
  async function logout() {
    if (activeRef.current) {
      setError("请先结束当前录音并确认音频状态后退出。");
      return;
    }
    if (dirty && !window.confirm("当前文书尚未保存，是否放弃修改并退出？"))
      return;
    if (hasNativeBridge())
      try {
        await bridgeCall("session.clear");
      } catch (e) {
        handleError(e);
        return;
      }
    resetWorkspace();
    try { await signOut(authConfig); } catch (failure) { setError(errorMessage(failure)); }
  }
  const development =
    system?.synthetic === true ||
    system?.mode === "development" ||
    system?.environment === "development" ||
    system?.mode === "local" ||
    system?.development === true;
  if (initializing)
    return (
      <div className="startup">
        <HeartPulse size={32} />
        <Loading text={PUBLIC_DEMO ? "正在载入公开演示 · 虚构数据 · 只读" : "正在恢复院内工作台…"} />
      </div>
    );
  if (!user)
    return (
      <Login
        config={authConfig}
        onLogin={setUser}
        externalError={error}
        onClearError={() => setError("")}
      />
    );
  return (
    <div className={`app-shell${PUBLIC_DEMO ? " public-demo" : ""}`}>
      {PUBLIC_DEMO && <div className="public-demo-banner" role="status">
        <strong>公开演示 · 虚构数据 · 只读</strong>
        <span>未连接医院、ASR 或模型服务</span>
        <a href="https://github.com/chnoorlee/HIS-" target="_blank" rel="noreferrer">Yongzhi Li (chnoorlee) · 源码</a>
      </div>}
      <header className="topbar" ref={topbar}>
        <div className="brand">
          <IconButton
            label="展开患者列表"
            className="mobile-menu"
            onClick={() => setSidebar(!sidebar)}
          >
            <Menu size={19} />
          </IconButton>
          <div className="brand-icon">
            <HeartPulse size={23} />
          </div>
          <div>
            <strong>住院语音病历</strong>
            <span>临床文书工作台</span>
          </div>
        </div>
        <div className="hospital-label">
          <Hospital size={15} />
          <span>
            {user.hospital_id === "hospital_demo"
              ? "示范医院 · 虚构数据"
              : user.hospital_id}
          </span>
          {development && !PUBLIC_DEMO && <span className="environment-label">本地开发</span>}
        </div>
        <div className="account">
          <span className={`connection-dot ${!online ? "red" : ""}`} />
          <span className="connection-label">
            {PUBLIC_DEMO ? "静态演示" : online ? "服务已连接" : "网络离线"}
          </span>
          <div className="account-divider" />
          <div className="avatar">
            {(user.display_name ?? user.username).slice(0, 1)}
          </div>
          <span>{user.display_name ?? user.username}</span>
          <IconButton label="退出登录" disabled={PUBLIC_DEMO} onClick={() => void logout()}>
            <LogOut size={17} />
          </IconButton>
        </div>
      </header>
      <div className="body-shell">
        {sidebar && (
          <button
            className="sidebar-backdrop"
            aria-label="收起患者列表"
            onClick={() => setSidebar(false)}
          />
        )}
        <aside className={`sidebar ${sidebar ? "mobile-open" : ""}`}>
          <div className="department-heading">
            <span className="inline gap">
              <Users size={16} />
              {encounter?.department ?? "在院患者"}
            </span>
            <IconButton
              label="收起患者列表"
              className="mobile-menu"
              onClick={() => setSidebar(false)}
            >
              <PanelLeftClose size={17} />
            </IconButton>
          </div>
          <div className="patient-list-heading">
            <h2>在院患者</h2>
            <span className="count-badge">{encounters.length}</span>
          </div>
          <div className="patient-search">
            <Search size={16} />
            <input
              aria-label="搜索患者"
              placeholder="姓名、床号、住院号"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            {search && (
              <IconButton label="清除搜索" onClick={() => setSearch("")}>
                <X size={13} />
              </IconButton>
            )}
          </div>
          <div className="patient-list">
            {listLoading ? (
              <Loading text="查询患者…" />
            ) : encounters.length ? (
              encounters.map((item) => (
                <button
                  key={item.id}
                  className={`patient-row ${encounter?.id === item.id ? "selected" : ""}`}
                  onClick={() => selectEncounter(item)}
                >
                  <div className="patient-row-top">
                    <span className="bed-label">{item.bed} 床</span>
                    <strong>{item.patient_name}</strong>
                    <span className="patient-demographic">
                      {item.sex} · {item.age}岁
                    </span>
                  </div>
                  <p>{item.diagnosis || "诊断待补充"}</p>
                  <div className="patient-row-bottom">
                    <span>{item.admission_id}</span>
                    {encounter?.id === item.id ? (
                      <span className="current-label">当前就诊</span>
                    ) : (
                      <ChevronRight size={13} />
                    )}
                  </div>
                </button>
              ))
            ) : (
              <Empty
                icon={<Search size={24} />}
                title="没有匹配患者"
                description="仅显示当前账号获准访问的就诊。"
              />
            )}
          </div>
          <div className="sidebar-footer">
            <ShieldCheck size={15} />
            <span>{PUBLIC_DEMO ? "公开虚构样例 · 无医院权限" : "医院与就诊权限控制"}</span>
          </div>
        </aside>
        <main className="main-content">
          {!PUBLIC_DEMO && !online && (
            <div className="offline-banner" role="alert">
              <WifiOff size={16} />
              当前网络离线，录音与提交状态需要恢复连接后核实。
            </div>
          )}
          <nav className="main-nav" aria-label="工作台导航">
            {NAV_ITEMS.filter(
              (item) => item.id !== "settings" || user.roles.includes("admin"),
            ).map((item) => (
              <button
                className={page === item.id ? "selected" : ""}
                key={item.id}
                  onClick={() => {
                    if(activeRef.current && item.id !== 'workspace') {
                      setError('请先结束当前录音，再切换工作台页面。');
                      return;
                    }
                    if (dirty && item.id !== "workspace") {
                    setError("请先保存当前文书修改再切换页面。");
                    return;
                  }
                  setPage(item.id);
                }}
              >
                <item.icon size={16} />
                {item.label}
              </button>
            ))}
            <span className="nav-date">
              {new Date().toLocaleDateString("zh-CN", {
                month: "long",
                day: "numeric",
                weekday: "short",
              })}
            </span>
          </nav>
          {encounter ? (
            <>
              <section className="patient-header">
                <div className="patient-identity">
                  <span className="large-bed">
                    <BedDouble size={19} />
                    {encounter.bed} 床
                  </span>
                  <div>
                    <div className="inline gap">
                      <h1>{encounter.patient_name}</h1>
                      <span>
                        {encounter.sex} · {encounter.age}岁
                      </span>
                      <span className="tag good">在院</span>
                    </div>
                    <p>{encounter.diagnosis || "初步诊断待补充"}</p>
                  </div>
                </div>
                <div className="encounter-identifiers">
                  <div>
                    <span>住院号</span>
                    <strong>{encounter.admission_id}</strong>
                  </div>
                  <div>
                    <span>入院时间</span>
                    <strong>{encounter.admitted_at?.slice(0, 10)}</strong>
                  </div>
                  <IconButton
                    label="刷新当前就诊"
                    disabled={loading}
                    onClick={() => void refresh().catch(handleError)}
                  >
                    <RefreshCw size={17} className={loading ? "spin" : ""} />
                  </IconButton>
                </div>
              </section>
              {page === "workspace" ? (
                <div className="workspace-page">
                  {loading ? (
                    <Loading text="读取就诊来源与文书…" />
                  ) : (
                    <>
                      <Recording
                        key={encounter.id}
                        encounterId={encounter.id}
                        patientName={encounter.patient_name}
                        sessions={sessions}
                        session={session}
                        onSelect={selectSession}
                        onRefresh={refresh}
                        onError={handleError}
                        onActive={setCaptureActive}
                        development={development}
                      />
                      <div className="clinical-grid">
                        <NoteEditor
                          key={encounter.id}
                          encounterId={encounter.id}
                          sessionId={session?.id}
                          patientName={encounter.patient_name}
                          notes={notes}
                          note={note}
                          onSelect={selectNote}
                          onRefresh={refresh}
                          onError={handleError}
                          onFact={(id, sessionId) => setSelectedFact({ id, sessionId })}
                          onDirty={setNoteDirty}
                        />
                        <Evidence
                          key={session?.id ?? encounter.id}
                          sessionId={session?.id}
                          extractionReady={system?.fact_extraction_ready === true}
                          transcripts={transcripts}
                          facts={facts}
                          sources={sources}
                          onRefresh={refresh}
                          onError={handleError}
                          selectedFact={selectedFact}
                        />
                      </div>
                    </>
                  )}
                </div>
              ) : page === "sources" ? (
                <section className="page-section">
                  <div className="page-title">
                    <div>
                      <h2>院内资料快照</h2>
                      <p>
                        就诊 {encounter.admission_id} · {sources.length}{" "}
                        份有版本来源
                      </p>
                    </div>
                    <button
                      className="secondary"
                      disabled={PUBLIC_DEMO}
                      onClick={() =>
                        void post(`/encounters/${encounter.id}/sources/refresh`)
                          .then(refresh)
                          .catch(handleError)
                      }
                    >
                      <RefreshCw size={15} />
                      同步院内资料
                    </button>
                  </div>
                  {sources.length ? (
                    <div className="source-table table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>资料名称</th>
                            <th>来源系统</th>
                            <th>版本</th>
                            <th>临床时间</th>
                            <th>状态</th>
                            <th />
                          </tr>
                        </thead>
                        <tbody>
                          {sources.map((item) => (
                            <tr key={item.id}>
                              <td>
                                <button
                                  className="table-link"
                                  onClick={() => setSource(item)}
                                >
                                  <FileText size={16} />
                                  {item.title ?? "院内来源"}
                                </button>
                              </td>
                              <td>{item.source_system ?? "未记录"}</td>
                              <td>v{item.version ?? 1}</td>
                              <td>
                                {dateText(
                                  item.clinical_time ?? item.created_at,
                                )}
                              </td>
                              <td>
                                <Status value={item.status} />
                              </td>
                              <td>
                                <IconButton
                                  label="查看来源详情"
                                  onClick={() => setSource(item)}
                                >
                                  <ChevronRight size={16} />
                                </IconButton>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <Empty
                      icon={<Building2 size={35} />}
                      title="尚未获取院内资料"
                      description="同步失败或未获取均不代表临床阴性。"
                    />
                  )}
                </section>
              ) : page === "activity" ? (
                <ActivityPage
                  encounterId={encounter.id}
                  onError={handleError}
                />
              ) : page === "audit" ? (
                <AuditPage onError={handleError} />
              ) : (
                <SettingsPage system={system} onError={handleError} />
              )}
            </>
          ) : (
            <Empty
              icon={<Users size={40} />}
              title="请选择获准访问的住院就诊"
            />
          )}
        </main>
      </div>
      {error && (
        <div className="error-toast" role="alert" ref={errorToast}>
          <AlertCircle size={20} />
          <div>
            <strong>操作未完成</strong>
            <p>{error}</p>
          </div>
          <IconButton label="关闭错误提示" onClick={() => setError("")}>
            <X size={17} />
          </IconButton>
        </div>
      )}
      {source && (
        <Modal
          title={source.title ?? "院内来源"}
          onClose={() => setSource(null)}
          wide
        >
          <div className="form-stack">
            <div className="inline gap">
              <Status value={source.status} />
              <span>来源版本 v{source.version}</span>
            </div>
            <StructuredData value={source.content ?? source.data ?? source} />
          </div>
        </Modal>
      )}
    </div>
  );
}
function Login({
  config,
  onLogin,
  externalError,
  onClearError,
}: {
  config: AuthConfig | null;
  onLogin: (user: User) => void;
  externalError: string;
  onClearError: () => void;
}) {
  const [username, setUsername] = useState(config?.environment === "development" ? "doctor" : "");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function login(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    onClearError();
    try {
      if (!config) throw new Error("无法读取身份认证配置，请重新加载页面。");
      if (config.mode === "oidc") { await signIn(config); return; }
      const result = await post<{ access_token: string; user?: User }>(
        "/auth/login",
        { username, password },
      );
      tokenStore.set(result.access_token);
      onLogin(result.user ?? (await api<User>("/auth/me")));
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-page">
      <header className="login-header">
        <HeartPulse size={26} />
        <strong>住院语音病历</strong>
        <span>院内临床文书系统</span>
      </header>
      <main className="login-main">
        <div className="login-form-wrap">
          <div className="login-symbol">
            <Hospital size={34} />
          </div>
          <h1>医生工作台</h1>
          <p className="login-subtitle">住院诊疗 · 语音记录 · 来源核查</p>
          <form className="login-form" onSubmit={(event) => void login(event)}>
            {config?.mode === "local" && <>
            <label>
              院内账号
              <input
                name="username"
                autoComplete="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                required
                autoFocus
              />
            </label>
            <label>
              密码
              <input
                name="password"
                type="password"
                autoComplete="current-password"
                placeholder="输入账号密码"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                required
              />
            </label>
            </>}
            {(error || externalError) && (
              <Notice kind="bad">{error || externalError}</Notice>
            )}
            <button className="primary login-submit" disabled={busy || !config}>
              {busy ? (
                <Loader2 className="spin" size={17} />
              ) : (
                <>
                  {config?.mode === "oidc" ? "医院统一身份登录" : "进入工作台"}
                  <ArrowRight size={17} />
                </>
              )}
            </button>
          </form>
          {!config && <button className="secondary" onClick={() => location.reload()}><RefreshCw size={16} />重新连接</button>}
          {config?.mode === "local" && config.environment !== "production" && <div className="login-dev">
            <span className="tag warn">本地开发账号</span>
            <div>
              <span>doctor</span>
              <code>Doctor123!</code>
            </div>
            <p>开发环境使用虚构就诊与演示模型。</p>
          </div>}
          <div className="login-security">
            <ShieldCheck size={15} />
            账号权限与访问行为均由服务端校验
          </div>
        </div>
      </main>
      <footer>正式病历签署与归档由医院 EMR 完成</footer>
    </div>
  );
}
function ActivityPage({
  encounterId,
  onError,
}: {
  encounterId: string;
  onError: (error: unknown) => void;
}) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [exports, setExports] = useState<ExportOperation[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<unknown>(null);
  const refresh = useCallback(async () => {
    const [j, e] = await Promise.all([
      api<Job[]>(`/jobs?encounter_id=${encounterId}`),
      api<ExportOperation[]>(`/exports?encounter_id=${encounterId}`),
    ]);
    setJobs(j);
    setExports(e);
  }, [encounterId]);
  useEffect(() => {
    void refresh()
      .catch(onError)
      .finally(() => setLoading(false));
    const timer = setInterval(() => void refresh().catch(onError), 6000);
    return () => clearInterval(timer);
  }, [refresh, onError]);
  return (
    <section className="page-section">
      <div className="page-title">
        <div>
          <h2>任务与写回记录</h2>
          <p>模型处理、持久任务和 EMR 写回状态</p>
        </div>
        <IconButton
          label="刷新任务状态"
          onClick={() => void refresh().catch(onError)}
        >
          <RefreshCw size={17} />
        </IconButton>
      </div>
      {loading ? (
        <Loading />
      ) : (
        <>
          <h3 className="table-section-title">文书与识别任务</h3>
          {jobs.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>任务</th>
                    <th>创建时间</th>
                    <th>状态</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((item) => (
                    <tr key={item.id}>
                      <td>
                        <strong>
                          {item.kind ?? item.job_type ?? "文书处理"}
                        </strong>
                        <small>{item.id}</small>
                      </td>
                      <td>{dateText(item.created_at)}</td>
                      <td>
                        <Status value={item.status} />
                      </td>
                      <td>
                        <IconButton
                          label="任务详情"
                          onClick={() => setSelected(item)}
                        >
                          <ChevronRight size={16} />
                        </IconButton>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty icon={<Activity size={30} />} title="暂无处理任务" />
          )}
          <h3 className="table-section-title">EMR 写回</h3>
          {exports.length ? (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>操作编号</th>
                    <th>文书版本</th>
                    <th>状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {exports.map((item) => (
                    <tr key={item.id}>
                      <td>{item.id}</td>
                      <td>v{item.note_revision ?? "?"}</td>
                      <td>
                        <Status value={item.status} />
                      </td>
                      <td>
                        {item.status === "UNKNOWN" ? (
                          <button
                            className="secondary"
                            disabled={PUBLIC_DEMO}
                            onClick={() =>
                              void post(`/exports/${item.id}/reconcile`)
                                .then(refresh)
                                .catch(onError)
                            }
                          >
                            <RefreshCw size={14} />
                            回读核实
                          </button>
                        ) : (
                          <IconButton
                            label="写回详情"
                            onClick={() => setSelected(item)}
                          >
                            <ChevronRight size={16} />
                          </IconButton>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty icon={<ListChecks size={30} />} title="暂无写回记录" />
          )}
        </>
      )}
      {selected !== null && (
        <Modal title="任务详情" onClose={() => setSelected(null)} wide>
          <div className="form-stack">
            <StructuredData value={selected} />
          </div>
        </Modal>
      )}
    </section>
  );
}
function AuditPage({ onError }: { onError: (error: unknown) => void }) {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const refresh = useCallback(async () => {
    setEntries(await api<AuditEntry[]>("/audit"));
  }, []);
  useEffect(() => {
    void refresh()
      .catch(onError)
      .finally(() => setLoading(false));
  }, [refresh, onError]);
  const filtered = entries.filter((item) =>
    `${item.action} ${item.actor_id ?? item.user_id} ${item.entity_id}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  return (
    <section className="page-section">
      <div className="page-title">
        <div>
          <h2>访问与操作审计</h2>
          <p>{PUBLIC_DEMO ? "预置虚构审计样例 · 不记录访客操作" : "当前账号获准查看的操作记录"}</p>
        </div>
        <IconButton
          label="刷新审计记录"
          onClick={() => void refresh().catch(onError)}
        >
          <RefreshCw size={17} />
        </IconButton>
      </div>
      <div className="audit-search">
        <Search size={16} />
        <input
          aria-label="筛选审计记录"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="筛选操作、账号或对象"
        />
      </div>
      {loading ? (
        <Loading />
      ) : filtered.length ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>操作</th>
                <th>操作者</th>
                <th>关联对象</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((item) => (
                <tr key={item.id}>
                  <td>{dateText(item.created_at)}</td>
                  <td>
                    <span className="audit-action">{item.action}</span>
                  </td>
                  <td>{item.actor_id ?? item.user_id ?? "系统"}</td>
                  <td>
                    <span className="mono">{item.entity_id ?? "—"}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty icon={<ShieldCheck size={30} />} title="暂无匹配审计记录" />
      )}
    </section>
  );
}
function SettingsPage({
  system,
  onError,
}: {
  system: SystemStatus | null;
  onError: (error: unknown) => void;
}) {
  const [settings, setSettings] = useState<Record<string, unknown>>({});
  const [incidents, setIncidents] = useState<unknown[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const editableKeys=['export_enabled','mock_emr_scenario','audio_retention_hours','content_retention_days','recovery_isolation'];
  useEffect(() => {
    void Promise.all([
      api<Record<string, unknown>>("/admin/settings"),
      api<unknown[]>("/admin/incidents"),
    ])
      .then(([s, i]) => {
        setSettings(s);
        setIncidents(i);
      })
      .catch(onError)
      .finally(() => setLoading(false));
  }, [onError]);
  async function save() {
    setBusy(true);
    setSaved(false);
    try {
      setSettings(
        await patch<Record<string, unknown>>("/admin/settings", Object.fromEntries(Object.entries(settings).filter(([key])=>editableKeys.includes(key)))),
      );
      setSaved(true);
    } catch (error) {
      onError(error);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="page-section">
      <div className="page-title">
        <div>
          <h2>系统设置</h2>
          <p>{PUBLIC_DEMO ? "公开演示配置 · 未连接医院服务" : "医院范围配置 · 仅管理员可变更"}</p>
        </div>
        <button
          className="primary"
          disabled={PUBLIC_DEMO || busy || loading}
          onClick={() => void save()}
        >
          <Settings2 size={15} />
          保存配置
        </button>
      </div>
      {saved && <Notice kind="good">配置已保存，变更已记录审计。</Notice>}
      {loading ? (
        <Loading />
      ) : (
        <div className="settings-layout">
          <div>
            <h3 className="table-section-title">运行策略</h3>
            <div className="settings-form">
              {Object.entries(settings).filter(([key])=>editableKeys.includes(key)).map(([key, value]) => (
                <label className="setting-row" key={key}>
                  <span>
                    {(
                      {
                        audio_retention_hours: "音频保留小时数",
                        content_retention_days: "内容保留天数",
                        export_enabled: "允许 EMR 草稿写回",
                        recovery_isolation: "恢复数据进入隔离",
                        mock_emr_scenario: "模拟 EMR 场景",
                        transcript_retention_days: "转写保留天数",
                        offline_grace_seconds: "离线宽限秒数",
                        mock_connector_scenario: "模拟连接器场景",
                        connector_scenario: "模拟连接器场景",
                        allow_offline_capture: "允许授权会话离线采音",
                      } as Record<string, string>
                    )[key] ?? key}
                  </span>
                  {typeof value === "boolean" ? (
                    <input
                      type="checkbox"
                      disabled={PUBLIC_DEMO}
                      checked={value}
                      onChange={(event) =>
                        setSettings({
                          ...settings,
                          [key]: event.target.checked,
                        })
                      }
                    />
                  ) : typeof value === "number" ? (
                    <input
                      type="number"
                      disabled={PUBLIC_DEMO}
                      min={0}
                      value={value}
                      onChange={(event) =>
                        setSettings({
                          ...settings,
                          [key]: Number(event.target.value),
                        })
                      }
                    />
                  ) : key.includes("scenario") ? (
                    <select
                      disabled={PUBLIC_DEMO}
                      value={String(value)}
                      onChange={(event) =>
                        setSettings({ ...settings, [key]: event.target.value })
                      }
                    >
                      <option value="normal">正常写回</option>
                      <option value="timeout_after_commit">提交后超时</option>
                      <option value="timeout_before_commit">提交前超时</option>
                      <option value="query_not_found">回读未查到记录</option>
                      <option value="reject">远端拒绝</option>
                      <option value="signed_conflict">已签署文书冲突</option>
                    </select>
                  ) : typeof value === "string" ? (
                    <input
                      readOnly={PUBLIC_DEMO}
                      value={value}
                      onChange={(event) =>
                        setSettings({ ...settings, [key]: event.target.value })
                      }
                    />
                  ) : (
                    <StructuredData value={value} />
                  )}
                </label>
              ))}
            </div>
            <h3 className="table-section-title">运行状态</h3>
            <StructuredData value={system} />
          </div>
          <div>
            <h3 className="table-section-title">隔离与生命周期事件</h3>
            {incidents.length ? (
              <StructuredData value={incidents} />
            ) : (
              <Empty icon={<ShieldCheck size={28} />} title="暂无隔离事件" />
            )}
          </div>
        </div>
      )}
    </section>
  );
}
