interface BridgeMessage {
  type: string;
  request_id?: string;
  payload?: Record<string, unknown>;
}
interface WebView {
  postMessage: (message: BridgeMessage) => void;
  addEventListener: (
    type: "message",
    listener: (event: MessageEvent<BridgeMessage>) => void,
  ) => void;
  removeEventListener: (
    type: "message",
    listener: (event: MessageEvent<BridgeMessage>) => void,
  ) => void;
}
declare global {
  interface Window {
    chrome?: { webview?: WebView };
  }
}
export interface CaptureEnd {
  channel_id: string;
  capture_epoch: string | number;
  last_seq: number;
  sample_end: number;
}
export interface Device {
  id: string;
  name: string;
  state: string;
  is_default: boolean;
}
export const hasNativeBridge = () => Boolean(window.chrome?.webview);
export class BridgeTimeoutError extends Error {}
export function bridgeCall<T>(
  type: string,
  payload: Record<string, unknown> = {},
): Promise<T> {
  const webview = window.chrome?.webview;
  if (!webview)
    return Promise.reject(new Error("当前环境没有 Windows 采音宿主。"));
  return new Promise((resolve, reject) => {
    const requestId = crypto.randomUUID();
    const timeout = window.setTimeout(() => {
      webview.removeEventListener("message", listener);
      reject(new BridgeTimeoutError("桌面采音响应超时，客户端操作可能仍在进行，请保持当前患者并核查采音状态。"));
    }, 30000);
    const listener = (event: MessageEvent<BridgeMessage>) => {
      if (event.data.request_id !== requestId) return;
      clearTimeout(timeout);
      webview.removeEventListener("message", listener);
      if (event.data.type === "bridge.error")
        reject(
          new Error(String(event.data.payload?.message ?? "桌面采音操作失败")),
        );
      else resolve(event.data.payload as T);
    };
    webview.addEventListener("message", listener);
    webview.postMessage({ type, request_id: requestId, payload });
  });
}
export function subscribeBridge(callback: (event: BridgeMessage) => void) {
  const webview = window.chrome?.webview;
  const listener = (event: MessageEvent<BridgeMessage>) => callback(event.data);
  webview?.addEventListener("message", listener);
  return () => webview?.removeEventListener("message", listener);
}
