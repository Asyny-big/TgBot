/**
 * Session lifecycle: login, cold-start restore through the refresh cookie, and
 * the token never leaving memory.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { stubApi, usePiniaForEachTest } from "./helpers";

const apiStub = stubApi();
const apiClientStub = { post: vi.fn(), get: vi.fn() };
const setAccessToken = vi.fn();
let expireSession: (() => void) | null = null;

// The API module is replaced, but the real ApiError is reused: the store must
// behave the same way against the error type production actually throws.
vi.mock("@/api", async () => {
  const { ApiError } = await import("@/api/client");
  return {
    api: apiStub,
    apiClient: apiClientStub,
    setAccessToken,
    getAccessToken: vi.fn(),
    onSessionExpired: (handler: () => void) => {
      expireSession = handler;
    },
    ApiError,
  };
});

const { useAuthStore } = await import("@/stores/auth");
const { ApiError } = await import("@/api");

usePiniaForEachTest();

beforeEach(() => {
  vi.clearAllMocks();
  expireSession = null;
});

describe("auth store", () => {
  it("signs in and remembers who the administrator is", async () => {
    apiStub.auth.login.mockResolvedValue({
      access_token: "access-1",
      refresh_token: "refresh-1",
      token_type: "bearer",
      access_expires_in: 900,
      refresh_expires_in: 604800,
    });
    apiStub.auth.me.mockResolvedValue({ username: "admin" });
    const auth = useAuthStore();

    const ok = await auth.login("admin", "secret");

    expect(ok).toBe(true);
    expect(auth.isAuthenticated).toBe(true);
    expect(auth.username).toBe("admin");
    expect(setAccessToken).toHaveBeenCalledWith("access-1");
    // Nothing was written to browser storage.
    expect(window.localStorage.length).toBe(0);
    expect(document.cookie).toBe("");
  });

  it("reports a rejected login and stays signed out", async () => {
    apiStub.auth.login.mockRejectedValue(
      new ApiError(401, "invalid_credentials", "Invalid username or password"),
    );
    const auth = useAuthStore();

    const ok = await auth.login("admin", "wrong");

    expect(ok).toBe(false);
    expect(auth.isAuthenticated).toBe(false);
    expect(auth.error).toBe("Invalid username or password");
  });

  it("restores a session from the refresh cookie on a cold start", async () => {
    apiClientStub.post.mockResolvedValue({ access_token: "access-2" });
    apiStub.auth.me.mockResolvedValue({ username: "admin" });
    const auth = useAuthStore();

    const ok = await auth.restore();

    expect(ok).toBe(true);
    expect(apiClientStub.post).toHaveBeenCalledWith("/auth/refresh", {});
    expect(auth.username).toBe("admin");
  });

  it("stays signed out when there is no valid refresh cookie", async () => {
    apiClientStub.post.mockRejectedValue(
      new ApiError(401, "invalid_token", "Invalid or expired token"),
    );
    const auth = useAuthStore();

    const ok = await auth.restore();

    expect(ok).toBe(false);
    expect(auth.isAuthenticated).toBe(false);
  });

  it("logging out clears the token even if the request fails", async () => {
    apiStub.auth.login.mockResolvedValue({
      access_token: "access-1",
      refresh_token: "refresh-1",
      token_type: "bearer",
      access_expires_in: 900,
      refresh_expires_in: 604800,
    });
    apiStub.auth.me.mockResolvedValue({ username: "admin" });
    apiStub.auth.logout.mockRejectedValue(new Error("network"));
    const auth = useAuthStore();
    await auth.login("admin", "secret");

    await expect(auth.logout()).rejects.toThrow("network");
    expect(auth.isAuthenticated).toBe(false);
    expect(setAccessToken).toHaveBeenLastCalledWith(null);
  });

  it("an expired session from the client ends the store session too", async () => {
    apiStub.auth.login.mockResolvedValue({
      access_token: "access-1",
      refresh_token: "refresh-1",
      token_type: "bearer",
      access_expires_in: 900,
      refresh_expires_in: 604800,
    });
    apiStub.auth.me.mockResolvedValue({ username: "admin" });
    const auth = useAuthStore();
    await auth.login("admin", "secret");

    expect(expireSession).not.toBeNull();
    expireSession?.();

    expect(auth.isAuthenticated).toBe(false);
    expect(auth.username).toBeNull();
  });
});
