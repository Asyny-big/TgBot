/** Transient notifications: one place, so every view reports errors the same way. */

import { defineStore } from "pinia";
import { ref } from "vue";

import { ApiError } from "@/api";

export type ToastKind = "success" | "error" | "info";

export interface Toast {
  id: number;
  kind: ToastKind;
  text: string;
}

const DISMISS_AFTER_MS = 6000;

export const useToastStore = defineStore("toasts", () => {
  const items = ref<Toast[]>([]);
  let nextId = 1;

  function dismiss(id: number): void {
    items.value = items.value.filter((toast) => toast.id !== id);
  }

  function push(kind: ToastKind, text: string): void {
    const id = nextId++;
    items.value = [...items.value, { id, kind, text }];
    if (typeof window !== "undefined") {
      window.setTimeout(() => dismiss(id), DISMISS_AFTER_MS);
    }
  }

  const success = (text: string) => push("success", text);
  const info = (text: string) => push("info", text);
  const error = (text: string) => push("error", text);

  /** Report a caught error, using the API's message and field details. */
  function reportError(caught: unknown, fallback = "Что-то пошло не так"): void {
    if (caught instanceof ApiError) {
      const fields = caught.fieldErrors;
      const detail = fields.length > 0
        ? fields.map((item) => `${item.field}: ${item.message}`).join("; ")
        : caught.message;
      error(detail);
      return;
    }
    error(caught instanceof Error ? caught.message : fallback);
  }

  return { items, push, success, info, error, reportError, dismiss };
});
