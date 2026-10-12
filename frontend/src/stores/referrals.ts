/**
 * Referral relationships and the referral programme's configuration.
 *
 * Read only, deliberately. The percentages are process configuration, validated
 * once when the bot starts, so there is nothing here to edit — the panel reports
 * what the running shop is doing and who has earned what.
 */

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { api } from "@/api";
import type { ReferralRecordResponse, ReferralSettingsResponse } from "@/api/endpoints";
import { useToastStore } from "@/stores/toasts";

export const REFERRAL_PAGE_SIZES = [20, 50, 100] as const;

export const useReferralStore = defineStore("referrals", () => {
  const toasts = useToastStore();

  const items = ref<ReferralRecordResponse[]>([]);
  const settings = ref<ReferralSettingsResponse | null>(null);
  const total = ref(0);
  const limit = ref<number>(REFERRAL_PAGE_SIZES[0]);
  const offset = ref(0);
  const search = ref("");
  const loading = ref(false);
  const loaded = ref(false);

  let inFlight: AbortController | null = null;

  const page = computed(() => Math.floor(offset.value / limit.value) + 1);
  const pages = computed(() => Math.max(1, Math.ceil(total.value / limit.value)));
  const isEmpty = computed(() => loaded.value && items.value.length === 0);

  async function fetchPage(): Promise<void> {
    // A newer request supersedes an older one, so a fast typist never sees the
    // results of a query they have already moved past.
    inFlight?.abort();
    const controller = new AbortController();
    inFlight = controller;
    loading.value = true;
    try {
      const result = await api.referrals.list({
        limit: limit.value,
        offset: offset.value,
        search: search.value.trim() || undefined,
      });
      items.value = result.items;
      total.value = result.meta.total;
      loaded.value = true;
    } catch (caught) {
      if (controller.signal.aborted || (caught instanceof Error && caught.name === "AbortError")) {
        return;
      }
      toasts.reportError(caught, "Не удалось загрузить рефералов");
    } finally {
      if (inFlight === controller) {
        inFlight = null;
        loading.value = false;
      }
    }
  }

  async function fetchSettings(): Promise<void> {
    try {
      settings.value = await api.referrals.settings();
    } catch (caught) {
      toasts.reportError(caught, "Не удалось загрузить настройки referral");
    }
  }

  async function applyFilters(next: { search?: string; limit?: number }): Promise<void> {
    if (next.search !== undefined) {
      search.value = next.search;
    }
    if (next.limit !== undefined) {
      limit.value = next.limit;
    }
    offset.value = 0;
    await fetchPage();
  }

  async function goTo(nextOffset: number): Promise<void> {
    offset.value = Math.max(0, nextOffset);
    await fetchPage();
  }

  return {
    items,
    settings,
    total,
    limit,
    offset,
    search,
    loading,
    loaded,
    page,
    pages,
    isEmpty,
    fetchPage,
    fetchSettings,
    applyFilters,
    goTo,
  };
});
