export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: Record<string, unknown>,
  ) {
    super(message);
  }
}
let authGeneration = 0;
const requests = new Set<AbortController>();
function invalidateRequests() {
  authGeneration += 1;
  requests.forEach((controller) => controller.abort());
  requests.clear();
}
export const tokenStore = {
  get: () => sessionStorage.getItem("his_access_token"),
  generation: () => authGeneration,
  set: (token: string) => {
    invalidateRequests();
    sessionStorage.setItem("his_access_token", token);
  },
  clear: () => {
    invalidateRequests();
    sessionStorage.removeItem("his_access_token");
  },
};
export const isCancelled = (error: unknown) =>
  error instanceof DOMException && error.name === "AbortError";
export async function authenticatedFetch(
  path: string,
  options: RequestInit = {},
): Promise<Response> {
  const token = tokenStore.get();
  const generation = authGeneration;
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (options.signal?.aborted) controller.abort();
  options.signal?.addEventListener("abort", abort, { once: true });
  requests.add(controller);
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, {
      ...options,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...options.headers,
      },
    });
    if (generation !== authGeneration) throw new DOMException("Identity changed", "AbortError");
    if (response.status === 401 && token) {
      tokenStore.clear();
      window.dispatchEvent(new Event("his-auth-expired"));
    }
    return response;
  } catch (error) {
    if (controller.signal.aborted || isCancelled(error)) throw new DOMException("Request cancelled", "AbortError");
    throw new ApiError(
      0,
      "offline",
      "连接中断，请检查院内网络。未保存的编辑仍保留在当前页面。",
    );
  } finally {
    requests.delete(controller);
    options.signal?.removeEventListener("abort", abort);
  }
}
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const generation = authGeneration;
  const response = await authenticatedFetch(path, options);
  const data = await response.json().catch(() => null);
  if (generation !== authGeneration && response.status !== 401)
    throw new DOMException("Identity changed", "AbortError");
  if (!response.ok) {
    const detail = data?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((d: { msg: string }) => d.msg).join("；")
          : (detail?.message ?? `服务请求失败（${response.status}）`);
    throw new ApiError(
      response.status,
      detail?.code ?? "request_failed",
      message,
      detail,
    );
  }
  return data as T;
}
export const post = <T>(path: string, data: unknown = {}) =>
  api<T>(path, { method: "POST", body: JSON.stringify(data) });
export const patch = <T>(path: string, data: unknown) =>
  api<T>(path, { method: "PATCH", body: JSON.stringify(data) });
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "操作未完成，请重试。";
}
