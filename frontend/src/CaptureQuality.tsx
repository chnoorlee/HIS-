import { useEffect, useState } from "react";
import { subscribeBridge } from "./bridge";
import { qualityStatus, updateQuality, type ChannelQuality } from "./capture-quality";
import "./capture-quality.css";

export default function CaptureQuality({ native, sessionId, mode, state }: {
  native: boolean; sessionId?: string; mode?: string; state: string;
}) {
  const [readings, setReadings] = useState<Record<string, ChannelQuality | undefined>>({});
  const [startedAt, setStartedAt] = useState(0);
  const [clock, setClock] = useState(0);
  const recording = native && !!sessionId && state === "RECORDING";
  useEffect(() => {
    if (!recording) return;
    const started = performance.now();
    setStartedAt(started);
    setClock(started);
    setReadings({});
    const unsubscribe = subscribeBridge(event => {
      if (event.type !== "capture.quality") return;
      const payload = event.payload ?? {};
      if (payload.session_id && payload.session_id !== sessionId) return;
      const channel = payload.channel_id;
      if (channel !== "doctor_mic" && (mode !== "conversation" || channel !== "patient_mic")) return;
      const at = performance.now();
      setReadings(previous => ({ ...previous, [channel]: updateQuality(previous[channel], payload, at) }));
      setClock(at);
    });
    const timer = window.setInterval(() => setClock(performance.now()), 1000);
    return () => { unsubscribe(); window.clearInterval(timer); };
  }, [recording, sessionId, mode]);
  if (!recording) return null;
  const channels = mode === "conversation" ? ["doctor_mic", "patient_mic"] : ["doctor_mic"];
  return <div className="capture-quality" aria-label="麦克风分通道电平">
    {channels.map(channel => {
      const label = channel === "doctor_mic" ? "医生声道" : "患者声道";
      const reading = readings[channel];
      const status = qualityStatus(reading, startedAt, clock);
      const stale = status.label === "未收到电平更新";
      return <div key={channel} className="channel-quality" data-channel={channel}>
        <span>{label}</span>
        <span className={`channel-quality-status ${status.kind}`} role="status">{status.label}</span>
        <meter aria-label={`${label}输入电平`} min={0} max={1} value={stale ? 0 : reading?.rms ?? 0} />
        <span className="channel-quality-clipping">削波 {stale || !reading ? "未知" : `${(reading.clipping * 100).toFixed(1)}%`}</span>
      </div>;
    })}
  </div>;
}
