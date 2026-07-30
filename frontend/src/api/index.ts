/**
 * Single place where the API client is constructed.
 *
 * The token hooks are wired to the auth store lazily, so the client can be
 * created before Pinia exists (and replaced wholesale in tests).
 */

import { ApiClient } from "@/api/client";
import { createApi } from "@/api/endpoints";
import type { Api } from "@/api/endpoints";

const API_PREFIX = "/api/v1";

let accessToken: string | null = null;
let sessionExpiredHandler: (() => void) | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function onSessionExpired(handler: () => void): void {
  sessionExpiredHandler = handler;
}

const baseUrl = `${import.meta.env.VITE_API_BASE_URL ?? ""}${API_PREFIX}`;

export const apiClient = new ApiClient(baseUrl, {
  getToken: () => accessToken,
  setToken: setAccessToken,
  onSessionExpired: () => sessionExpiredHandler?.(),
});

export const api: Api = createApi(apiClient);

export { ApiError } from "@/api/client";
