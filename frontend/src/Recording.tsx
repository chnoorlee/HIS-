import { useEffect, useRef, useState } from "react";
import {
  CircleStop,
  Mic,
  Pause,
  Play,
  Plus,
  RefreshCw,
  ShieldAlert,
  Waves,
  Monitor,
  FlaskConical,
} from "lucide-react";
import { api, isCancelled, post, tokenStore } from "./api";
import {
  bridgeCall,
  BridgeTimeoutError,
  hasNativeBridge,
  subscribeBridge,
  type CaptureEnd,
  type Device,
} from "./bridge";
import { BrowserCapture, type CaptureState } from "./capture";
import CaptureQuality from "./CaptureQuality";
import { IconButton, Modal, Notice, Status, dateText } from "./components";
import type { Session } from "./types";
interface CaptureResult {
  channels: CaptureEnd[];
  pending_chunks: number;
  pending_gaps?: number;
  finalization_blocked?: boolean;
  reason?: string;
}
export default function Recording({
  encounterId,
  patientName,
  sessions,
  session,
  onSelect,
  onRefresh,
  onError,
  onActive,
  development,
}: {
  encounterId: string;
  patientName: string;
  sessions: Session[];
  session: Session | null;
  onSelect: (session: Session) => void;
  onRefresh: () => Promise<void>;
  onError: (error: unknown) => void;
  onActive: (active: boolean) => void;
  development: boolean;
}) {
  const [native] = useState(hasNativeBridge);
  const [devices, setDevices] = useState<Device[]>([]);
  const [doctorDevice, setDoctorDevice] = useState("");
  const [patientDevice, setPatientDevice] = useState("");
  const [mode, setMode] = useState("dictation");
  const [busy, setBusy] = useState(false);
  const [showNew, setShowNew] = useState(false);
  const [showQuarantine, setShowQuarantine] = useState(false);
  const [reason, setReason] = useState("");
  const [consentRecord, setConsentRecord] = useState("");
  const [capture, setCapture] = useState<CaptureState>({
    state: "IDLE",
    level: 0,
    pending: 0,
    durable: 0,
    message: "",
  });
  const [seconds, setSeconds] = useState(0);
  const browser = useRef<BrowserCapture | null>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const active = ["STARTING", "RECORDING", "PAUSED", "ERROR", "FINALIZING"].includes(capture.state);
  const channels = useRef<CaptureEnd[]>([]);
  const lifecycle = useRef(0);
  const mounted = useRef(true);
  const finalizing = useRef(false);
  useEffect(() => {
    onActive(active);
  }, [active, onActive]);
  useEffect(() => {
    if (capture.state !== "RECORDING") return;
    const timer = setInterval(() => setSeconds((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [capture.state]);
  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    const ctx = c.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, c.width, c.height);
    for (let i = 0; i < 45; i++) {
      const h =
        capture.state === "RECORDING"
          ? Math.max(
              3,
              Math.min(
                34,
                capture.level *
                  180 *
                  (0.3 + Math.abs(Math.sin(i * 1.7 + seconds))),
              ),
            )
          : 3;
      ctx.fillStyle = capture.state === "RECORDING" ? "#138577" : "#bdc8c3";
      ctx.fillRect(i * 5, (c.height - h) / 2, 2, h);
    }
  }, [capture.level, capture.state, seconds]);
  useEffect(() => {
    if (!native) return;
    return subscribeBridge((event) => {
      const p = event.payload ?? {};
      if (p.session_id && p.session_id !== session?.id) return;
      if (event.type === "capture.state")
        setCapture((old) => ({
          ...old,
          state: p.finalization_blocked ? "ERROR" : finalizing.current ? "FINALIZING" : String(p.state).toUpperCase(),
          message: p.finalization_blocked ? "最终音频未能保存，不能提交结束声明。请核查本地存储。" : String(p.reason ?? ""),
        }));
      if (event.type === "capture.quality")
        setCapture((old) => ({ ...old, level: Number(p.rms ?? 0) }));
      if (event.type === "capture.buffer")
        setCapture((old) => ({
          ...old,
          pending: Number(p.pending_chunks ?? 0),
        }));
      if (event.type === "capture.error")
        setCapture((old) => ({
          ...old,
          // The host publishes sampling transitions separately via capture.state.
          message: p.code === "upload_disconnected" ? "上传连接中断，音频保留在本地加密缓存，正在重连。"
            : p.code === "authorization_refresh_unavailable" ? "授权暂未刷新，采音将在现有授权宽限期结束后暂停。"
            : p.code === "audio_gap_reported" ? "本地缺失音频已登记，请核查会话音频清单。"
            : p.code === "local_retention_expired" ? "过期缓存音频已清理并登记缺失范围，请核查会话音频清单。"
            : String(p.message ?? "采音错误"),
        }));
    });
  }, [native, session?.id]);
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => {
      if (active) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [active]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      lifecycle.current += 1;
      void browser.current?.dispose();
      if (native) void bridgeCall("capture.pause").catch(() => undefined);
    };
  }, [native]);
  async function run(action: () => Promise<void>) {
    setBusy(true);
    try {
      await action();
    } catch (error) {
      if (mounted.current && !isCancelled(error)) onError(error);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  async function loadDevices() {
    if (native) {
      const result = await bridgeCall<{ devices: Device[] }>("devices.list");
      setDevices(result.devices);
      setDoctorDevice(
        result.devices.find((device) => device.is_default)?.id ??
          result.devices[0]?.id ??
          "",
      );
    } else {
      if (!navigator.mediaDevices) throw new Error("当前环境不可访问麦克风");
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((track) => track.stop());
      const list = await navigator.mediaDevices.enumerateDevices();
      setDevices(
        list
          .filter((device) => device.kind === "audioinput")
          .map((device, index) => ({
            id: device.deviceId,
            name: device.label || `麦克风 ${index + 1}`,
            state: "active",
            is_default: device.deviceId === "default",
          })),
      );
    }
  }
  async function openNew() {
    setShowNew(true);
    await run(loadDevices);
  }
  async function create() {
    const generation = lifecycle.current;
    await run(async () => {
      const created = await post<Session>("/sessions", {
        encounter_id: encounterId,
        mode,
        ...(consentRecord.trim() ? { consent_record: consentRecord.trim() } : {}),
      });
      if (!mounted.current || generation !== lifecycle.current) return;
      onSelect(created);
      setShowNew(false);
      setSeconds(0);
      channels.current = [];
      finalizing.current = false;
      setCapture({
        state: "IDLE",
        level: 0,
        pending: 0,
        durable: 0,
        message: "",
      });
      await onRefresh();
    });
  }
  async function start() {
    if (!session) return;
    const generation = lifecycle.current;
    const id = session.id;
    channels.current = [];
    onActive(true);
    setCapture((old) => ({ ...old, state: "STARTING", message: "" }));
    await run(async () => {
      try {
      if (native) {
        await bridgeCall("capture.start", {
          token: tokenStore.get(),
          session_id: id,
          mode: session.mode,
          channels: [
            { device_id: doctorDevice, channel_id: "doctor_mic" },
            ...(session.mode === "conversation"
              ? [{ device_id: patientDevice, channel_id: "patient_mic" }]
              : []),
          ],
        });
        if (!mounted.current || generation !== lifecycle.current) {
          await bridgeCall("capture.pause").catch(() => undefined);
          return;
        }
        setCapture((old) => ({ ...old, state: "RECORDING" }));
      } else {
        if (session.mode === "conversation")
          throw new Error("医患独立双通道采音需要 Windows 客户端。");
        const manifest = await api<{ channels: unknown[]; final_manifest: unknown[] }>(`/sessions/${id}/audio-manifest`);
        if (!mounted.current || generation !== lifecycle.current) return;
        if (session.status !== "CREATED" || manifest.channels.length || manifest.final_manifest.length)
          throw new Error("当前页面缺少此会话的原始结束边界，无法续录。请新建会话；原生缓存须在 Windows 客户端恢复并核查。");
        const cap = new BrowserCapture(id, (value) => {
          if (!mounted.current || generation !== lifecycle.current) return;
          if (finalizing.current) setCapture((old) => ({ ...value, state: old.state === "ERROR" ? "ERROR" : "FINALIZING" }));
          else setCapture(value);
        });
        browser.current = cap;
        await cap.start(doctorDevice || undefined);
      }
      if (!mounted.current || generation !== lifecycle.current) return;
      await onRefresh().catch(onError);
      } catch (error) {
        if (mounted.current && generation === lifecycle.current) {
          finalizing.current = native;
          setCapture((old) => ({ ...old, state: native ? "ERROR" : "IDLE", message: native ? "桌面采音结果尚未确认，当前患者保持锁定。请重试归档或隔离会话。" : old.message }));
          onActive(native);
          if (native) void bridgeCall("capture.pause").catch((pauseError) => { if (mounted.current) onError(pauseError); });
          browser.current = null;
        }
        throw error;
      }
    });
  }
  async function pauseResume() {
    if (!session) return;
    const generation = lifecycle.current;
    await run(async () => {
      const pause = capture.state === "RECORDING";
      if (!pause) await post(`/sessions/${session.id}/resume`);
      if (!mounted.current || generation !== lifecycle.current) return;
      try {
        if (native) await bridgeCall(pause ? "capture.pause" : "capture.resume");
        else if (pause) browser.current?.pause();
        else await browser.current?.resume();
      } catch (error) {
        if (!mounted.current || generation !== lifecycle.current) return;
        if (!pause) await post(`/sessions/${session.id}/pause`).catch(onError);
        throw error;
      }
      if (!mounted.current || generation !== lifecycle.current) return;
      if (pause) await post(`/sessions/${session.id}/pause`);
      if (!mounted.current || generation !== lifecycle.current) return;
      setCapture((old) => ({ ...old, state: pause ? "PAUSED" : "RECORDING" }));
      await onRefresh();
    });
  }
  async function stop() {
    if (!session) return;
    const generation = lifecycle.current;
    finalizing.current = true;
    onActive(true);
    setCapture((old) => ({ ...old, state: "FINALIZING", message: "" }));
    await run(async () => {
      try {
      const result: CaptureResult | undefined = channels.current.length ? { channels: channels.current, pending_chunks: 0 } : native
        ? await bridgeCall<CaptureResult>(
            "capture.stop",
          )
        : await browser.current?.stop();
      if (!mounted.current || generation !== lifecycle.current) return;
      if (!result) throw new Error("未找到当前采音状态");
      if (result.finalization_blocked) throw new Error("最终音频未能保存，不能提交结束声明。请核查本地存储。");
      const incomplete = result.pending_chunks > 0 || (result.pending_gaps ?? 0) > 0;
      if (!incomplete) channels.current = result.channels;
      setCapture((old) => ({
        ...old,
        pending: result.pending_chunks,
        state: incomplete ? "ERROR" : "FINALIZING",
        message: incomplete
          ? "仍有音频未取得持久化确认，请保持当前页面并恢复连接。"
          : "",
      }));
      if (incomplete)
        throw new Error("存在未确认音频，尚未提交完整结束声明。");
      const finalized = await post<Session>(`/sessions/${session.id}/finalize`, {
        channels: result.channels,
      });
      if (!mounted.current || generation !== lifecycle.current) return;
      if (finalized.status !== "COMPLETE") throw new Error("音频结束声明仍存在缺片，尚未完成归档。请核查音频清单。");
      finalizing.current = false;
      channels.current = [];
      setCapture((old) => ({ ...old, state: "STOPPED", message: "" }));
      onActive(false);
      browser.current = null;
      await onRefresh().catch(onError);
      } catch (error) {
        if (!mounted.current || generation !== lifecycle.current) return;
        setCapture((old) => ({ ...old, state: "ERROR", message: channels.current.length ? "音频已确认，结束声明尚未成功。请重试归档。" : old.message || (error instanceof Error ? error.message : String(error)) }));
        throw error;
      }
    });
  }
  async function quarantine() {
    if (!session || !reason.trim()) return;
    const generation = lifecycle.current;
    await run(async () => {
      if (active) {
        if (native) await bridgeCall("capture.pause");
        else browser.current?.pause();
      }
      if (!mounted.current || generation !== lifecycle.current) return;
      await post(`/sessions/${session.id}/quarantine`, { reason });
      if (!mounted.current || generation !== lifecycle.current) return;
      await browser.current?.dispose();
      if (!mounted.current || generation !== lifecycle.current) return;
      browser.current = null;
      setCapture((old) => ({ ...old, state: "STOPPED", message: "" }));
      finalizing.current = false;
      channels.current = [];
      onActive(false);
      setShowQuarantine(false);
      setReason("");
      await onRefresh();
    });
  }
  const canStart =
    session &&
    (native ? ["CREATED", "PAUSED", "RECORDING"].includes(session.status.toUpperCase()) : session.status === "CREATED") &&
    !active;
  return (
    <section className="recording-section" aria-label="采音会话">
      <div className="recording-head">
        <div className="inline gap">
          <span className="section-marker">
            <Mic size={15} />
          </span>
          <strong>语音采集</strong>
          <span className="subtle">
            {native ? "Windows 原生采音" : "浏览器 · 单通道"}
          </span>
        </div>
        <div className="inline">
          <select
            aria-label="当前录音会话"
            className="session-select"
            value={session?.id ?? ""}
            disabled={active || busy}
            onChange={(event) => {
              const found = sessions.find((s) => s.id === event.target.value);
              if (found) onSelect(found);
            }}
          >
            <option value="" disabled>
              暂无录音会话
            </option>
            {sessions.map((item) => (
              <option value={item.id} key={item.id}>
                {dateText(item.created_at)} ·{" "}
                {item.id.slice(-6)}
              </option>
            ))}
          </select>
          <IconButton
            label="新建录音会话"
            disabled={active || busy}
            onClick={() => void openNew()}
          >
            <Plus size={18} />
          </IconButton>
        </div>
      </div>
      <div className="recording-controls">
        <div
          className={`mic-orbit ${capture.state === "RECORDING" ? "is-live" : ""}`}
        >
          <Mic size={22} />
        </div>
        <div className="recording-label">
          <strong>
            {capture.state === "RECORDING"
              ? "正在采集"
              : capture.state === "STARTING" ? "正在连接麦克风"
              : capture.state === "FINALIZING" ? "正在确认归档"
              : active
                ? "采集已暂停"
                : session
                  ? "当前会话"
                  : "等待新建会话"}
          </strong>
          <span>
            {session
              ? `${patientName} · ${session.mode === "conversation" ? "医患对话" : "医生口述"}`
              : "未绑定录音对象"}
          </span>
        </div>
        <canvas
          className="waveform"
          ref={canvas}
          width="225"
          height="40"
          aria-label="实时音量波形"
        />
        <span className="recording-time">
          {String(Math.floor(seconds / 60)).padStart(2, "0")}:
          {String(seconds % 60).padStart(2, "0")}
        </span>
        <div className="inline gap recording-actions">
          {!active && (
            <button
              className="primary"
              disabled={!canStart || busy}
              onClick={() => void start()}
            >
              <Mic size={16} />
              开始录音
            </button>
          )}
          {active && (
            <>
              <IconButton
                label={capture.state === "RECORDING" ? "暂停录音" : "恢复录音"}
                disabled={busy || finalizing.current || channels.current.length > 0}
                onClick={() => void pauseResume()}
              >
                {capture.state === "RECORDING" ? (
                  <Pause size={18} />
                ) : (
                  <Play size={18} />
                )}
              </IconButton>
              <button
                className="secondary"
                disabled={busy}
                onClick={() => void stop()}
              >
                <CircleStop size={16} />
                {finalizing.current ? "重试归档" : "结束"}
              </button>
            </>
          )}
          {session && (
            <IconButton
              label="隔离误录会话"
              disabled={busy || session.status === "QUARANTINED"}
              onClick={() => setShowQuarantine(true)}
            >
              <ShieldAlert size={17} />
            </IconButton>
          )}
          {native && session && !active && <IconButton label="恢复本会话本地音频" disabled={busy || session.status === "QUARANTINED"} onClick={() => void run(async () => {
            const generation = lifecycle.current;
            finalizing.current = true;
            onActive(true);
            setCapture((old) => ({ ...old, state: "STARTING", message: "" }));
            let blocked = false;
            try {
              const result = await bridgeCall<CaptureResult>("capture.recover", { token: tokenStore.get(), session_id: session.id });
              if (!mounted.current || generation !== lifecycle.current) return;
              blocked = result.finalization_blocked === true;
              if (result.finalization_blocked) throw new Error("最终音频未能保存，请核查本地存储后处理。");
              const incomplete = result.pending_chunks > 0 || (result.pending_gaps ?? 0) > 0;
              channels.current = incomplete ? [] : result.channels;
              setCapture((old) => ({ ...old, state: "PAUSED", pending: result.pending_chunks, message: result.reason === "recovery_requires_review" || (result.pending_gaps ?? 0) > 0 ? "本地音频与服务端清单尚未核实，请保留缓存并交管理员核查。" : "已恢复本会话音频，请确认结束归档。" }));
            } catch (error) {
              if (!mounted.current || generation !== lifecycle.current) return;
              const uncertain = blocked || error instanceof BridgeTimeoutError;
              finalizing.current = uncertain;
              onActive(uncertain);
              setCapture((old) => ({ ...old, state: uncertain ? "ERROR" : "IDLE", message: uncertain ? "本地恢复结果尚未确认，当前患者保持锁定。请重试归档或隔离会话。" : old.message }));
              if (uncertain) void bridgeCall("capture.pause").catch((pauseError) => { if (mounted.current) onError(pauseError); });
              throw error;
            }
          })}><RefreshCw size={16} /></IconButton>}
        </div>
      </div>
      <CaptureQuality native={native} sessionId={session?.id} mode={session?.mode} state={capture.state} />
      <div className="recording-foot">
        <span className="inline gap">
          <span
            className={`connection-dot ${capture.pending ? "amber" : ""}`}
          />
          {capture.pending
            ? `${capture.pending} 片待持久化确认`
            : `${capture.durable} 片已持久化确认`}
        </span>
        <span>
          {session ? <Status value={session.status} /> : <span>尚未采集</span>}
        </span>
        <span className="recording-disclaimer">
          {native ? "原生加密缓冲" : "内存缓冲 · 关闭页面可能丢失未确认音频"}
        </span>
      </div>
      {capture.message && <Notice kind="bad">{capture.message}</Notice>}
      {!native && session && !active && ["PAUSED", "RECORDING", "INCOMPLETE", "FINALIZING"].includes(session.status) && (
        <Notice>当前页面未保留此会话的原始结束边界，不能继续录音或声明音频完整。请新建会话；如有原生缓存，请在 Windows 客户端恢复并核查。</Notice>
      )}
      {development &&
        session &&
        !active &&
        session.status !== "QUARANTINED" && (
          <div className="fixture-action">
            <button
              className="text-button"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  await post(`/sessions/${session.id}/demo-script`);
                  await onRefresh();
                })
              }
            >
              <FlaskConical size={14} />
              载入虚构口述脚本
            </button>
          </div>
        )}
      {showNew && (
        <Modal title="新建采音会话" onClose={() => setShowNew(false)}>
          <div className="form-stack">
            <Notice kind="info">
              会话固定绑定：{patientName}。就诊标识 {encounterId}。
            </Notice>
            <label>
              采集同意记录
              <textarea rows={2} value={consentRecord} onChange={(event) => setConsentRecord(event.target.value)} placeholder="填写院内授权或知情同意记录编号与核查说明" />
            </label>
            <label>
              采集模式
              <select
                value={mode}
                onChange={(event) => setMode(event.target.value)}
              >
                <option value="dictation">医生补充口述</option>
                <option value="conversation" disabled={!native}>
                  医患对话 · 独立双通道
                </option>
              </select>
            </label>
            <label>
              医生麦克风
              <div className="inline gap">
                <select
                  value={doctorDevice}
                  onChange={(event) => setDoctorDevice(event.target.value)}
                >
                  <option value="">系统默认设备</option>
                  {devices.map((device) => (
                    <option value={device.id} key={device.id}>
                      {device.name}
                    </option>
                  ))}
                </select>
                <IconButton
                  label="刷新麦克风设备"
                  disabled={busy}
                  onClick={() => void run(loadDevices)}
                >
                  <RefreshCw size={16} />
                </IconButton>
              </div>
            </label>
            {mode === "conversation" && (
              <label>
                患者麦克风
                <select
                  value={patientDevice}
                  onChange={(event) => setPatientDevice(event.target.value)}
                >
                  <option value="">选择患者独立设备</option>
                  {devices
                    .filter((device) => device.id !== doctorDevice)
                    .map((device) => (
                      <option value={device.id} key={device.id}>
                        {device.name}
                      </option>
                    ))}
                </select>
              </label>
            )}
            <div className="muted inline gap">
              {native ? <Monitor size={16} /> : <Waves size={16} />}{" "}
              {native
                ? "桌面采音 · 加密本地缓存"
                : "浏览器采音 · 单通道 · 当前页面内存缓冲"}
            </div>
            <div className="modal-actions">
              <button className="secondary" onClick={() => setShowNew(false)}>
                取消
              </button>
              <button
                className="primary"
                disabled={
                  busy ||
                  (!development && !consentRecord.trim()) ||
                  (native && !doctorDevice) ||
                  (mode === "conversation" && !patientDevice)
                }
                onClick={() => void create()}
              >
                建立会话
              </button>
            </div>
          </div>
        </Modal>
      )}
      {showQuarantine && (
        <Modal title="隔离误录会话" onClose={() => setShowQuarantine(false)}>
          <div className="form-stack">
            <Notice kind="warn">
              本会话及关联派生材料将进入隔离状态，不能继续审核或写回。
            </Notice>
            <label>
              隔离原因
              <textarea
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="填写误录、错误患者绑定或其他原因"
              />
            </label>
            <div className="modal-actions">
              <button
                className="secondary"
                onClick={() => setShowQuarantine(false)}
              >
                取消
              </button>
              <button
                className="danger"
                disabled={busy || !reason.trim()}
                onClick={() => void quarantine()}
              >
                确认隔离
              </button>
            </div>
          </div>
        </Modal>
      )}
    </section>
  );
}
