export type ApiEnvelope<T> = {
  data: T;
  meta: Record<string, number | string | boolean | null>;
};

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function cookie(name: string): string | undefined {
  return document.cookie
    .split("; ")
    .find((entry) => entry.startsWith(`${name}=`))
    ?.split("=")
    .slice(1)
    .join("=");
}

export async function api<T>(
  path: string,
  options: Omit<RequestInit, "body"> & { body?: unknown } = {},
): Promise<ApiEnvelope<T>> {
  const headers = new Headers(options.headers);
  const isBodyObject = options.body !== undefined && !(options.body instanceof FormData);
  if (isBodyObject) headers.set("Content-Type", "application/json");
  const method = (options.method ?? "GET").toUpperCase();
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrf = cookie("photosweep_csrf");
    if (csrf) headers.set("X-CSRF-Token", decodeURIComponent(csrf));
  }
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    credentials: "same-origin",
    headers,
    body: isBodyObject ? JSON.stringify(options.body) : (options.body as BodyInit | null),
  });
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try {
      const problem = await response.json();
      if (typeof problem.detail === "string") detail = problem.detail;
      else if (Array.isArray(problem.detail)) detail = problem.detail.map((item: { msg?: string }) => item.msg ?? "参数不符合要求").join("；");
    } catch {
      // Preserve the useful status-based fallback.
    }
    if (response.status === 401 && !["/auth/login", "/auth/me", "/setup/admin"].includes(path)) window.dispatchEvent(new Event("photosweep:session-expired"));
    throw new ApiError(response.status, detail);
  }
  return response.json();
}

export function formatBytes(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** exponent;
  return `${value >= 10 || exponent === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[exponent]}`;
}

export function mediaThumbnailUrl(media: { id: number; size: number; mtime_ns: number }, size: 256 | 512 = 512): string {
  return `/api/v1/media/${media.id}/thumbnail?size=${size}&v=${media.size}-${media.mtime_ns}`;
}

export function formatDate(value?: string | number): string {
  if (!value) return "未知";
  const date = typeof value === "number" ? new Date(value / 1_000_000) : new Date(value);
  if (Number.isNaN(date.getTime())) return "未知";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
