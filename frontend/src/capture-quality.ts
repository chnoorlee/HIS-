export interface ChannelQuality {
  rms: number;
  clipping: number;
  receivedAt: number;
  quietSince?: number;
  clippingSince?: number;
}

export function updateQuality(previous: ChannelQuality | undefined, payload: Record<string, unknown>, at: number): ChannelQuality | undefined {
  const rms = payload.rms, clipping = payload.clipping_fraction;
  if (typeof rms !== "number" || !Number.isFinite(rms) || rms < 0 || rms > 1
    || typeof clipping !== "number" || !Number.isFinite(clipping) || clipping < 0 || clipping > 1) return previous;
  const continuous = previous && at - previous.receivedAt <= 2500 ? previous : undefined;
  return {
    rms, clipping, receivedAt: at,
    quietSince: rms < 0.005 ? continuous?.quietSince ?? at : undefined,
    clippingSince: clipping >= 0.01 ? continuous?.clippingSince ?? at : undefined,
  };
}

export function qualityStatus(reading: ChannelQuality | undefined, startedAt: number, at: number) {
  if (at - (reading?.receivedAt ?? startedAt) > 4000) return { kind: "warn", label: "未收到电平更新" };
  if (!reading) return { kind: "muted", label: "等待电平" };
  if (reading.clippingSince !== undefined && reading.receivedAt - reading.clippingSince >= 2000) return { kind: "bad", label: "持续削波" };
  if (reading.quietSince !== undefined && reading.receivedAt - reading.quietSince >= 8000) return { kind: "warn", label: "持续低电平" };
  return reading.rms < 0.005 ? { kind: "muted", label: "低电平" } : { kind: "normal", label: "有输入" };
}
