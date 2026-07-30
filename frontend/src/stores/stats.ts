/** Dashboard state: one request, refreshed on demand. */

import { defineStore } from "pinia";
import { ref } from "vue";

import { api } from "@/api";
import type { OverviewResponse } from "@/api/endpoints";
import { useToastStore } from "@/stores/toasts";

export const useStatsStore = defineStore("stats", () => {
  const toasts = useToastStore();
  const overview = ref<OverviewResponse | null>(null);
  const loading = ref(false);

  async function load(): Promise<void> {
    loading.value = true;
    try {
      overview.value = await api.stats.overview();
    } catch (caught) {
      toasts.reportError(caught, "Не удалось загрузить статистику");
    } finally {
      loading.value = false;
    }
  }

  return { overview, loading, load };
});
