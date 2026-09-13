import { useEffect, useRef, useState } from "react";
import { fetchEventSource } from "@microsoft/fetch-event-source";
import { ApiError, isCancelled, tokenStore } from "./api";

export interface PartialTranscript {
  run_id: string;
  segment_id: string;
  revision: number;
  text: string;
  channel_id: string;
  capture_epoch: string | number;
}
const segmentKey = (item: PartialTranscript) =>
  JSON.stringify([item.run_id, item.channel_id, String(item.capture_epoch), item.segment_id]);

export function useSessionEvents(sessionId: string | undefined, refresh: () => Promise<void>, onError: (error: unknown) => void) {
  const [partials, setPartials] = useState<PartialTranscript[]>([]);
  const [connection, setConnection] = useState("连接中");
  const callbacks = useRef({ refresh, onError });
  callbacks.current = { refresh, onError };
  useEffect(() => {
    setPartials([]);
    if (!sessionId) return;
    const controller = new AbortController();
    const generation = tokenStore.generation();
    const segments = new Map<string, PartialTranscript>();
    const finalized = new Map<string, number>();
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    const live = () => !controller.signal.aborted && generation === tokenStore.generation();
    const scheduleRefresh = () => {
      if (refreshTimer) return;
      refreshTimer = setTimeout(() => {
        refreshTimer = undefined;
        if (live()) void callbacks.current.refresh().catch(callbacks.current.onError);
      }, 180);
    };
    const revoke = () => {
      tokenStore.clear();
      window.dispatchEvent(new Event("his-auth-expired"));
      controller.abort();
    };
    void fetchEventSource(`/api/v1/sessions/${encodeURIComponent(sessionId)}/events`, {
      headers: { Authorization: `Bearer ${tokenStore.get()}` },
      signal: controller.signal,
      openWhenHidden: true,
      async onopen(response) {
        if (response.status === 401 || response.status === 403) { revoke(); throw new ApiError(response.status, "session_access", "会话访问已失效"); }
        if (!response.ok || !response.headers.get("content-type")?.includes("text/event-stream"))
          throw new ApiError(response.status, "event_stream", "实时转写连接不可用");
        if (live()) setConnection("实时连接");
      },
      onmessage(message) {
        if (!live() || !message.data) return;
        if (message.event === "access.revoked") { revoke(); return; }
        if (message.event === "session.quarantined" || message.event === "session.deleted") {
          setPartials([]);
          void callbacks.current.refresh().catch(callbacks.current.onError);
          controller.abort();
          return;
        }
        const data: PartialTranscript = JSON.parse(message.data);
        if (message.event === "asr.partial" || message.event === "asr.final") {
          if (typeof data.run_id !== "string" || typeof data.segment_id !== "string" || typeof data.text !== "string" || typeof data.channel_id !== "string" || !Number.isInteger(data.revision) || data.capture_epoch == null) return;
          const key = segmentKey(data);
          if (message.event === "asr.final") {
            finalized.set(key, Math.max(finalized.get(key) ?? -1, data.revision));
            segments.delete(key);
            scheduleRefresh();
          } else if (!finalized.has(key) && data.revision > (segments.get(key)?.revision ?? -1)) segments.set(key, data);
          setPartials([...segments.values()]);
        } else if (/^(transcript\.|facts\.|note\.|sources\.|job\.finished|session\.|asr\.(completed|failed|reconciliation_required))/.test(message.event)) scheduleRefresh();
      },
      onclose() { if (live()) throw new Error("实时连接已断开"); },
      onerror(error) {
        if (!live() || isCancelled(error)) throw error;
        setConnection("正在重连");
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) throw error;
        return 2500;
      },
    }).catch((error) => { if (live()) callbacks.current.onError(error); });
    return () => { controller.abort(); clearTimeout(refreshTimer); };
  }, [sessionId]);
  return { partials, connection };
}
