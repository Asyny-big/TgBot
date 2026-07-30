/**
 * The HTTP client is the only place that talks to the network, so its contract
 * is tested directly: auth header, refresh-and-retry, single-flight refresh,
 * error envelope parsing and query building.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "@/api/client";

interface Call {
  url: string;
  init: RequestInit;
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function envelope(code: string, message: string, details?: Record<string, unknown>): Response {
  return jsonResponse(401, { error: { code, message, details } });
}

describe("ApiClient", () => {
  let calls: Call[];
  let token: string | null;
  let sessionExpired: number;

  const build = (responses: Response[]) => {
    const queue = [...responses];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      calls.push({ url: String(input), init });
      const next = queue.shift();
      if (!next) {
        throw new Error(`unexpected request: ${String(input)}`);
      }
      return next;
    });
    vi.stubGlobal("fetch", fetchMock);
    return new ApiClient("/api/v1", {
      getToken: () => token,
      setToken: (value) => {
        token = value;
      },
      onSessionExpired: () => {
        sessionExpired += 1;
      },
    });
  };

  beforeEach(() => {
    calls = [];
    token = "access-1";
    sessionExpired = 0;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends the access token and parses JSON", async () => {
    const client = build([jsonResponse(200, { ok: true })]);

    const result = await client.get<{ ok: boolean }>("/auth/me");

    expect(result).toEqual({ ok: true });
    expect(calls[0]?.url).toBe("/api/v1/auth/me");
    expect((calls[0]?.init.headers as Record<string, string>).Authorization).toBe("Bearer access-1");
    expect(calls[0]?.init.credentials).toBe("include");
  });

  it("omits the Authorization header when there is no token", async () => {
    token = null;
    const client = build([jsonResponse(200, {})]);

    await client.get("/auth/me");

    expect((calls[0]?.init.headers as Record<string, string>).Authorization).toBeUndefined();
  });

  it("builds query strings and skips empty values", async () => {
    const client = build([jsonResponse(200, {})]);

    await client.get("/purchases", {
      query: { limit: 20, offset: 0, search: "", missing: undefined, status: ["paid", "delivered"] },
    });

    expect(calls[0]?.url).toBe("/api/v1/purchases?limit=20&offset=0&status=paid&status=delivered");
  });

  it("returns null for 204 responses", async () => {
    const client = build([new Response(null, { status: 204 })]);

    await expect(client.delete("/products/1")).resolves.toBeNull();
  });

  it("refreshes once on 401 and replays the original request", async () => {
    const client = build([
      envelope("invalid_token", "Invalid or expired token"),
      jsonResponse(200, { access_token: "access-2" }),
      jsonResponse(200, { username: "admin" }),
    ]);

    const result = await client.get<{ username: string }>("/auth/me");

    expect(result).toEqual({ username: "admin" });
    expect(calls.map((call) => call.url)).toEqual([
      "/api/v1/auth/me",
      "/api/v1/auth/refresh",
      "/api/v1/auth/me",
    ]);
    // The replay carries the refreshed token.
    expect((calls[2]?.init.headers as Record<string, string>).Authorization).toBe(
      "Bearer access-2",
    );
    expect(token).toBe("access-2");
    expect(sessionExpired).toBe(0);
  });

  it("gives up and reports an expired session when the refresh fails", async () => {
    const client = build([
      envelope("invalid_token", "Invalid or expired token"),
      envelope("invalid_token", "Invalid or expired token"),
    ]);

    await expect(client.get("/auth/me")).rejects.toBeInstanceOf(ApiError);
    expect(token).toBeNull();
    expect(sessionExpired).toBe(1);
  });

  it("does not retry the refresh endpoint itself", async () => {
    const client = build([envelope("invalid_token", "Invalid or expired token")]);

    await expect(client.post("/auth/refresh", {})).rejects.toBeInstanceOf(ApiError);
    expect(calls).toHaveLength(1);
  });

  it("collapses parallel 401s into a single refresh", async () => {
    const client = build([
      envelope("invalid_token", "expired"),
      envelope("invalid_token", "expired"),
      jsonResponse(200, { access_token: "access-2" }),
      jsonResponse(200, { first: true }),
      jsonResponse(200, { second: true }),
    ]);

    const [first, second] = await Promise.all([
      client.get<{ first: boolean }>("/products"),
      client.get<{ second: boolean }>("/purchases"),
    ]);

    expect(first).toEqual({ first: true });
    expect(second).toEqual({ second: true });
    const refreshes = calls.filter((call) => call.url.endsWith("/auth/refresh"));
    expect(refreshes).toHaveLength(1);
  });

  it("turns the error envelope into a typed ApiError", async () => {
    const client = build([
      jsonResponse(409, {
        error: {
          code: "slug_already_exists",
          message: "A product with this slug already exists",
          details: { slug: "vip1" },
        },
      }),
    ]);

    await expect(client.post("/products", {})).rejects.toMatchObject({
      status: 409,
      code: "slug_already_exists",
      message: "A product with this slug already exists",
      details: { slug: "vip1" },
    });
  });

  it("exposes field level validation errors", async () => {
    const client = build([
      jsonResponse(422, {
        error: {
          code: "validation_error",
          message: "Request payload is invalid",
          details: {
            fields: [
              { field: "slug", type: "string_pattern_mismatch", message: "does not match" },
              { field: "price_stars", type: "greater_than", message: "must be > 0" },
            ],
          },
        },
      }),
    ]);

    try {
      await client.post("/products", {});
      expect.unreachable("the request must fail");
    } catch (caught) {
      expect(caught).toBeInstanceOf(ApiError);
      const error = caught as ApiError;
      expect(error.fieldErrors).toEqual([
        { field: "slug", message: "does not match" },
        { field: "price_stars", message: "must be > 0" },
      ]);
    }
  });

  it("falls back to a readable message for a non-envelope failure", async () => {
    const client = build([new Response("gateway down", { status: 502 })]);

    await expect(client.get("/stats/overview")).rejects.toMatchObject({
      status: 502,
      code: "http_error",
    });
  });
});
