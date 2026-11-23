/** A small, typed fetch wrapper: same-origin cookies, CSRF header on writes, RFC 9457 errors. */

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail?: string;
  readonly extra: Record<string, unknown>;

  constructor(status: number, body: Record<string, unknown>) {
    super(String(body.detail ?? body.title ?? `Request failed (${status})`));
    this.status = status;
    this.code = String(body.code ?? "error");
    this.detail = body.detail ? String(body.detail) : undefined;
    this.extra = body;
  }
}

function csrfToken(): string | undefined {
  const match = document.cookie.match(/(?:^|; )px_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1] ?? "") : undefined;
}

type Method = "GET" | "POST" | "PATCH" | "PUT" | "DELETE";

export async function request<T>(method: Method, path: string, body?: unknown, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const isForm = body instanceof FormData;
  if (body !== undefined && !isForm) headers["Content-Type"] = "application/json";
  if (method !== "GET") {
    const token = csrfToken();
    if (token) headers["X-CSRF-Token"] = token;
  }
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers,
    body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
    ...init,
  });
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  const data = text ? (JSON.parse(text) as unknown) : undefined;
  if (!response.ok) throw new ApiError(response.status, (data as Record<string, unknown>) ?? {});
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body ?? {}),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body ?? {}),
  delete: <T>(path: string) => request<T>("DELETE", path),
};

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong";
}
