// Calls to this server's /api. The browser holds only the HttpOnly session cookie; mutations carry the page's
// Origin (the server's guard checks it). Errors come as {"error": {"code", "message"}}.

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

let unauthorized: (() => void) | null = null;

/** Called when a call is refused for the sign-in (it expired, or ended in another tab): the page asks again
 *  whether it is signed in and shows the sign-in. Signing in itself is not such a call. */
export function onUnauthorized(handler: () => void) {
  unauthorized = handler;
}

/** For a call made without ``api`` (an upload's XHR) that the server refused for the sign-in. */
export function signInRefused() {
  unauthorized?.();
}

export async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.headers = { ...init.headers, "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError(0, "offline", "The server could not be reached.");
  }
  if (response.status === 204) return undefined as T;
  let text: string;
  try {
    text = await response.text();
  } catch { // the connection broke while the answer came: the call may have been done
    throw new ApiError(0, "offline", "The server's answer did not come through.");
  }
  let parsed: unknown = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = null;
  }
  if (response.status === 401 && !(method === "POST" && path === "/api/session")) unauthorized?.();
  if (!response.ok) {
    const error = (parsed as { error?: { code?: unknown; message?: unknown } } | null)?.error;
    throw new ApiError(
      response.status,
      typeof error?.code === "string" ? error.code : `http_${response.status}`,
      typeof error?.message === "string" ? error.message : response.statusText || "Request failed.",
    );
  }
  return parsed as T;
}

export const get = <T>(path: string) => api<T>("GET", path);
export const post = <T>(path: string, body?: unknown) => api<T>("POST", path, body ?? {});
export const put = <T>(path: string, body?: unknown) => api<T>("PUT", path, body ?? {});
export const del = <T>(path: string) => api<T>("DELETE", path);

export const enc = encodeURIComponent;
