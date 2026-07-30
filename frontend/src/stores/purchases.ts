/**
 * Purchase search state, plus the two support actions.
 *
 * `verify` is the "check payment" button: its report is kept per purchase so the
 * table can show what the check found without reloading the page.
 */

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { api } from "@/api";
import type {
  DeliveryAttemptResponse,
  PurchaseRecordResponse,
  PurchaseStatus,
  VerificationResponse,
} from "@/api/endpoints";
import { outcomeLabel } from "@/api/format";
import { useToastStore } from "@/stores/toasts";

export const PURCHASE_PAGE_SIZES = [20, 50, 100] as const;

export const usePurchaseStore = defineStore("purchases", () => {
  const toasts = useToastStore();

  const items = ref<PurchaseRecordResponse[]>([]);
  const total = ref(0);
  const limit = ref<number>(PURCHASE_PAGE_SIZES[0]);
  const offset = ref(0);
  const search = ref("");
  const status = ref<PurchaseStatus | null>(null);
  const loading = ref(false);
  const loaded = ref(false);
  const busyId = ref<string | null>(null);
  const reports = ref<Record<string, VerificationResponse>>({});

  let inFlight: AbortController | null = null;

  const page = computed(() => Math.floor(offset.value / limit.value) + 1);
  const pages = computed(() => Math.max(1, Math.ceil(total.value / limit.value)));
  const isEmpty = computed(() => loaded.value && items.value.length === 0);

  async function fetchPage(): Promise<void> {
    inFlight?.abort();
    const controller = new AbortController();
    inFlight = controller;
    loading.value = true;
    try {
      const result = await api.purchases.search({
        limit: limit.value,
        offset: offset.value,
        search: search.value.trim() || undefined,
        status: status.value ? [status.value] : undefined,
      });
      items.value = result.items;
      total.value = result.meta.total;
      loaded.value = true;
    } catch (caught) {
      if (controller.signal.aborted || (caught instanceof Error && caught.name === "AbortError")) {
        return;
      }
      toasts.reportError(caught, "Не удалось загрузить покупки");
    } finally {
      if (inFlight === controller) {
        inFlight = null;
        loading.value = false;
      }
    }
  }

  async function applyFilters(next: {
    search?: string;
    status?: PurchaseStatus | null;
    limit?: number;
  }): Promise<void> {
    if (next.search !== undefined) {
      search.value = next.search;
    }
    if (next.status !== undefined) {
      status.value = next.status;
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

  function replace(record: PurchaseRecordResponse): void {
    items.value = items.value.map((item) =>
      item.purchase.id === record.purchase.id ? record : item,
    );
  }

  /** "Check payment": idempotent on the server, so double clicks are harmless. */
  async function verify(record: PurchaseRecordResponse): Promise<VerificationResponse | null> {
    const id = record.purchase.id;
    busyId.value = id;
    try {
      const report = await api.purchases.verify(id);
      reports.value = { ...reports.value, [id]: report };
      replace({
        ...record,
        purchase: { ...record.purchase, status: report.status_after },
      });
      if (report.resolved) {
        toasts.success(outcomeLabel(report.outcome));
      } else {
        toasts.info(outcomeLabel(report.outcome));
      }
      return report;
    } catch (caught) {
      toasts.reportError(caught, "Не удалось проверить платёж");
      return null;
    } finally {
      busyId.value = null;
    }
  }

  async function resend(record: PurchaseRecordResponse): Promise<DeliveryAttemptResponse | null> {
    const id = record.purchase.id;
    busyId.value = id;
    try {
      const attempt = await api.purchases.resend(id);
      if (attempt.status === "failed") {
        toasts.error(attempt.error ?? "Отправить ссылку не удалось");
      } else {
        toasts.success("Ссылка отправлена покупателю");
      }
      return attempt;
    } catch (caught) {
      toasts.reportError(caught, "Не удалось отправить ссылку");
      return null;
    } finally {
      busyId.value = null;
    }
  }

  return {
    items,
    total,
    limit,
    offset,
    search,
    status,
    loading,
    loaded,
    busyId,
    reports,
    page,
    pages,
    isEmpty,
    fetchPage,
    applyFilters,
    goTo,
    verify,
    resend,
  };
});
