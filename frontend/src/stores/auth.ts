/**
 * Session state.
 *
 * The access token is held in memory only. On a page reload there is no token,
 * but the httpOnly refresh cookie is still there — so `restore()` asks the API
 * for a fresh pair and the administrator stays signed in without ever exposing
 * a long-lived token to JavaScript storage.
 */

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { ApiError, api, apiClient, onSessionExpired, setAccessToken } from "@/api";

export const useAuthStore = defineStore("auth", () => {
  const username = ref<string | null>(null);
  const token = ref<string | null>(null);
  const restoring = ref(false);
  const signingIn = ref(false);
  const error = ref<string | null>(null);

  const isAuthenticated = computed(() => token.value !== null);

  function applyToken(value: string | null): void {
    token.value = value;
    setAccessToken(value);
  }

  function reset(): void {
    applyToken(null);
    username.value = null;
  }

  async function login(name: string, password: string): Promise<boolean> {
    signingIn.value = true;
    error.value = null;
    try {
      const tokens = await api.auth.login(name, password);
      applyToken(tokens.access_token);
      const admin = await api.auth.me();
      username.value = admin.username;
      return true;
    } catch (caught) {
      reset();
      error.value =
        caught instanceof ApiError ? caught.message : "Не удалось войти. Попробуйте ещё раз.";
      return false;
    } finally {
      signingIn.value = false;
    }
  }

  /** Try to resume a session using the refresh cookie. */
  async function restore(): Promise<boolean> {
    if (restoring.value) {
      return isAuthenticated.value;
    }
    restoring.value = true;
    try {
      const tokens = await apiClient.post<{ access_token: string }>("/auth/refresh", {});
      applyToken(tokens.access_token);
      const admin = await api.auth.me();
      username.value = admin.username;
      return true;
    } catch {
      reset();
      return false;
    } finally {
      restoring.value = false;
    }
  }

  async function logout(): Promise<void> {
    try {
      await api.auth.logout();
    } finally {
      reset();
    }
  }

  // A refresh that fails mid-session ends the session everywhere at once.
  onSessionExpired(() => {
    reset();
  });

  return {
    username,
    token,
    restoring,
    signingIn,
    error,
    isAuthenticated,
    login,
    logout,
    restore,
  };
});
