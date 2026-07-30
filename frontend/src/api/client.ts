/**
 * HTTP client for the admin API.
 *
 * Three responsibilities, and nothing else:
 *
 * - attach the in-memory access token to every request;
 * - on `401`, refresh once through the httpOnly cookie and replay the request,
 *   collapsing parallel refreshes into a single call;
 * - turn the backend's error envelope into a typed `ApiError`.
 *
 * The access token never touches `localStorage`: it lives in a closure here and
 * in the Pinia store, so an XSS cannot read it from storage. The refresh token
 * is an httpOnly cookie that JavaScript cannot read at all.
 */

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /** Field-level validation messages, when the failure was a `422`. */
  get fieldErrors(): { field: string; message: string }[] {
    const fields = this.details.fields;
    if (!Array.isArray(fields)) {
      return [];
    }
    return fields.flatMap((item) => {
      if (typeof item !== "object" || item === null) {
        return [];
      }
      const record = item as Record<string, unknown>;
      return [
        {
          field: String(record.field ?? ""),
          message: String(record.message ?? ""),
        },
      ];
    });
  }
}

type TokenReader = () => string | null;
type TokenWriter = (token: string | null) => void;

export interface ClientHooks {
  /** Current access token, or `null` when not signed in. */
  getToken: TokenReader;
  /** Called with a new token after a successful refresh, or `null` on failure. */
  setToken: TokenWriter;
  /** Called when the session is definitively over (refresh failed). */
  onSessionExpired?: () => void;
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  /** JSON body; `undefined` sends no body. */
  body?: unknown;
  query?: Record<string, string | number | boolean | undefined | (string | number)[]>;
  /** Internal: prevents a refresh loop. */
  retryOnUnauthorized?: boolean;
  signal?: AbortSignal;
}

const REFRESH_PATH = "/auth/refresh";
const NO_CONTENT = 204;
const UNAUTHORIZED = 401;

export class ApiClient {
  private readonly baseUrl: string;
  private readonly hooks: ClientHooks;
  private refreshing: Promise<boolean> | null = null;

  constructor(baseUrl: string, hooks: ClientHooks) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.hooks = hooks;
  }

  async request<ResultT>(path: string, options: RequestOptions = {}): Promise<ResultT> {
    const { method = "GET", body, query, retryOnUnauthorized = true, signal } = options;
    const url = this.buildUrl(path, query);
    const headers: Record<string, string> = { Accept: "application/json" };
    const token = this.hooks.getToken();
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }

    const response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      // The refresh token cookie must travel with every request.
      credentials: "include",
      ...(signal ? { signal } : {}),
    });

    if (response.status === UNAUTHORIZED && retryOnUnauthorized && !path.startsWith(REFRESH_PATH)) {
      const refreshed = await this.refreshOnce();
      if (refreshed) {
        return this.request<ResultT>(path, { ...options, retryOnUnauthorized: false });
      }
      this.hooks.setToken(null);
      this.hooks.onSessionExpired?.();
    }

    if (!response.ok) {
      throw await this.toError(response);
    }
    return (await this.parse(response)) as ResultT;
  }

  get<ResultT>(path: string, options: Omit<RequestOptions, "method" | "body"> = {}) {
    return this.request<ResultT>(path, { ...options, method: "GET" });
  }

  post<ResultT>(path: string, body?: unknown, options: Omit<RequestOptions, "method"> = {}) {
    return this.request<ResultT>(path, { ...options, method: "POST", body });
  }

  patch<ResultT>(path: string, body: unknown, options: Omit<RequestOptions, "method"> = {}) {
    return this.request<ResultT>(path, { ...options, method: "PATCH", body });
  }

  delete<ResultT>(path: string, options: Omit<RequestOptions, "method" | "body"> = {}) {
    return this.request<ResultT>(path, { ...options, method: "DELETE" });
  }

  /**
   * Refresh the access token, at most once per burst of 401s.
   *
   * Parallel requests that all hit 401 share this single promise, so the
   * rotating refresh token is never spent twice.
   */
  private async refreshOnce(): Promise<boolean> {
    this.refreshing ??= this.performRefresh().finally(() => {
      this.refreshing = null;
    });
    return this.refreshing;
  }

  private async performRefresh(): Promise<boolean> {
    try {
      const tokens = await this.request<{ access_token: string }>(REFRESH_PATH, {
        method: "POST",
        body: {},
        retryOnUnauthorized: false,
      });
      this.hooks.setToken(tokens.access_token);
      return true;
    } catch {
      return false;
    }
  }

  private buildUrl(
    path: string,
    query: RequestOptions["query"],
  ): string {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(query ?? {})) {
      if (value === undefined || value === "") {
        continue;
      }
      if (Array.isArray(value)) {
        for (const item of value) {
          search.append(key, String(item));
        }
      } else {
        search.append(key, String(value));
      }
    }
    const suffix = search.size > 0 ? `?${search.toString()}` : "";
    return `${this.baseUrl}${path}${suffix}`;
  }

  private async parse(response: Response): Promise<unknown> {
    if (response.status === NO_CONTENT) {
      return null;
    }
    const text = await response.text();
    if (!text) {
      return null;
    }
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }

  private async toError(response: Response): Promise<ApiError> {
    const payload = await this.parse(response);
    if (
      typeof payload === "object" &&
      payload !== null &&
      "error" in payload &&
      typeof (payload as ApiErrorBody).error === "object"
    ) {
      const { code, message, details } = (payload as ApiErrorBody).error;
      return new ApiError(response.status, code, message, details ?? {});
    }
    return new ApiError(
      response.status,
      "http_error",
      `Запрос завершился ошибкой ${response.status}`,
    );
  }
}
