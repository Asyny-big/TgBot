/**
 * Daily Content in the admin panel: one store per page.
 *
 * Every write the panel may repeat carries an idempotency key, and the key is
 * kept until the server confirmed: a click retried after a network failure is
 * the same answer, the same "send it to me" — never a second one. A change to a
 * request names the version it was read at; when another tab got there first
 * the server says 409 and the page reloads what is current.
 */

import { defineStore } from "pinia";
import { ref } from "vue";

import { ApiError, api } from "@/api";
import { newIdempotencyKey } from "@/api/dailyFormat";
import type {
  DailyContentKind,
  DailyContentResponse,
  DailyContentStatus,
  DailyDeliveryDetailResponse,
  DailyDeliveryResponse,
  DailyDeliveryStatus,
  DailyLibraryActionName,
  DailyOverviewResponse,
  DailyRequestDetailResponse,
  DailyRequestResponse,
  DailyRequestStatus,
  DailySubscriberDetailResponse,
  DailySubscriberResponse,
  DailySubscriptionStatus,
} from "@/api/endpoints";
import { usePagedList } from "@/stores/paged";
import { useToastStore } from "@/stores/toasts";

const CONFLICT_TEXTS: Record<string, string> = {
  daily_request_version_conflict:
    "Заявку уже изменили в другой вкладке — показываю актуальную версию, проверьте и повторите.",
  daily_library_action_not_allowed:
    "Для текущего статуса материала это действие недоступно — список обновлён.",
  daily_content_duplicate: "Такой же материал уже есть в библиотеке — восстановление не нужно.",
  daily_no_owner: "Не задан аккаунт владельца Daily-бота (DAILY_ADMIN_TELEGRAM_IDS).",
  idempotency_key_reused: "Этот ответ уже был отправлен с другим текстом.",
  daily_reply_too_long: "Ответ не помещается в одно сообщение Telegram — сократите его.",
};

/** The panel's own words for the conflicts it expects; anything else as is. */
function report(caught: unknown, fallback: string): void {
  const toasts = useToastStore();
  if (caught instanceof ApiError && CONFLICT_TEXTS[caught.code]) {
    toasts.error(CONFLICT_TEXTS[caught.code]!);
    return;
  }
  toasts.reportError(caught, fallback);
}

function isConflict(caught: unknown): caught is ApiError {
  return caught instanceof ApiError && caught.status === 409;
}

// --------------------------------------------------------------------------- //
// Overview                                                                    //
// --------------------------------------------------------------------------- //

export const useDailyOverviewStore = defineStore("dailyOverview", () => {
  const toasts = useToastStore();
  const overview = ref<DailyOverviewResponse | null>(null);
  const loading = ref(false);

  async function load(): Promise<void> {
    loading.value = true;
    try {
      overview.value = await api.daily.overview();
    } catch (caught) {
      toasts.reportError(caught, "Не удалось загрузить сводку Daily");
    } finally {
      loading.value = false;
    }
  }

  return { overview, loading, load };
});

// --------------------------------------------------------------------------- //
// Library                                                                     //
// --------------------------------------------------------------------------- //

interface LibraryFilters {
  status?: DailyContentStatus | undefined;
  kind?: DailyContentKind | undefined;
  search?: string | undefined;
}

export const useDailyLibraryStore = defineStore("dailyLibrary", () => {
  const toasts = useToastStore();
  const list = usePagedList<DailyContentResponse, LibraryFilters>({
    load: (query) => api.daily.library(query),
    filters: {},
    errorText: "Не удалось загрузить библиотеку",
  });
  const busyId = ref<number | null>(null);
  /** Per item: the key of a "send it to me" not yet confirmed. */
  const sendKeys = new Map<number, string>();

  async function changeStatus(
    item: DailyContentResponse,
    action: DailyLibraryActionName,
  ): Promise<void> {
    busyId.value = item.id;
    try {
      const updated = await api.daily.changeStatus(item.id, action);
      list.replace((row) => row.id === item.id, updated);
      toasts.success(`Материал #${item.id}: готово`);
    } catch (caught) {
      report(caught, "Не удалось изменить материал");
      if (isConflict(caught)) {
        await list.fetchPage();
      }
    } finally {
      busyId.value = null;
    }
  }

  async function sendToMe(item: DailyContentResponse): Promise<void> {
    const key = sendKeys.get(item.id) ?? newIdempotencyKey();
    sendKeys.set(item.id, key);
    busyId.value = item.id;
    try {
      const result = await api.daily.sendLibraryItem(item.id, key);
      sendKeys.delete(item.id);
      toasts.success(
        result.created ? "Daily-бот пришлёт материал вам в Telegram" : "Уже отправляется",
      );
    } catch (caught) {
      report(caught, "Не удалось поставить отправку");
    } finally {
      busyId.value = null;
    }
  }

  return { ...list, busyId, changeStatus, sendToMe };
});

// --------------------------------------------------------------------------- //
// Subscribers                                                                 //
// --------------------------------------------------------------------------- //

interface SubscriberFilters {
  status?: DailySubscriptionStatus | undefined;
  blocked?: boolean | undefined;
  banned?: boolean | undefined;
  search?: string | undefined;
}

export const useDailySubscriberStore = defineStore("dailySubscribers", () => {
  const toasts = useToastStore();
  const list = usePagedList<DailySubscriberResponse, SubscriberFilters>({
    load: (query) => api.daily.subscribers(query),
    filters: {},
    errorText: "Не удалось загрузить подписчиков",
  });
  const detail = ref<DailySubscriberDetailResponse | null>(null);
  const busy = ref(false);

  async function open(userId: number): Promise<void> {
    try {
      detail.value = await api.daily.subscriber(userId);
    } catch (caught) {
      toasts.reportError(caught, "Не удалось открыть подписчика");
    }
  }

  function close(): void {
    detail.value = null;
  }

  /** Ban or unban: idempotent on the server, so a repeated click is harmless. */
  async function setBan(userId: number, banned: boolean): Promise<void> {
    busy.value = true;
    try {
      const updated = await api.daily.setRequestsBan(userId, banned);
      detail.value = updated;
      list.replace((row) => row.user_id === userId, updated.subscriber);
      toasts.success(banned ? "Пожелания запрещены" : "Пожелания разрешены");
    } catch (caught) {
      report(caught, "Не удалось изменить запрет");
    } finally {
      busy.value = false;
    }
  }

  return { ...list, detail, busy, open, close, setBan };
});

// --------------------------------------------------------------------------- //
// Deliveries                                                                  //
// --------------------------------------------------------------------------- //

interface DeliveryFilters {
  status?: DailyDeliveryStatus | undefined;
  userId?: number | undefined;
  date?: string | undefined;
}

export const useDailyDeliveryStore = defineStore("dailyDeliveries", () => {
  const toasts = useToastStore();
  const list = usePagedList<DailyDeliveryResponse, DeliveryFilters>({
    load: (query) => api.daily.deliveries(query),
    filters: {},
    errorText: "Не удалось загрузить доставки",
  });
  const detail = ref<DailyDeliveryDetailResponse | null>(null);

  async function open(deliveryId: number): Promise<void> {
    try {
      detail.value = await api.daily.delivery(deliveryId);
    } catch (caught) {
      toasts.reportError(caught, "Не удалось открыть доставку");
    }
  }

  function close(): void {
    detail.value = null;
  }

  return { ...list, detail, open, close };
});

// --------------------------------------------------------------------------- //
// Content requests                                                            //
// --------------------------------------------------------------------------- //

interface RequestFilters {
  status?: DailyRequestStatus | undefined;
  userId?: number | undefined;
}

export interface RequestChanges {
  status?: DailyRequestStatus;
  admin_note?: string | null;
}

export const useDailyRequestStore = defineStore("dailyRequests", () => {
  const toasts = useToastStore();
  const list = usePagedList<DailyRequestResponse, RequestFilters>({
    load: (query) => api.daily.requests(query),
    filters: {},
    errorText: "Не удалось загрузить пожелания",
  });
  const detail = ref<DailyRequestDetailResponse | null>(null);
  const saving = ref(false);
  const replying = ref(false);
  const sendingItemId = ref<number | null>(null);
  /** The answer not yet confirmed: its text and the key every retry reuses. */
  let pendingReply: { requestId: number; text: string; key: string } | null = null;
  const itemKeys = new Map<number, string>();

  async function open(requestId: number): Promise<void> {
    try {
      detail.value = await api.daily.request(requestId);
    } catch (caught) {
      toasts.reportError(caught, "Не удалось открыть пожелание");
    }
  }

  function close(): void {
    detail.value = null;
  }

  function refreshRow(updated: DailyRequestDetailResponse): void {
    list.replace((row) => row.id === updated.request.id, updated.request);
  }

  /** Save the status or the note against the version on screen. */
  async function save(changes: RequestChanges): Promise<boolean> {
    const current = detail.value;
    if (current === null) {
      return false;
    }
    saving.value = true;
    try {
      const updated = await api.daily.updateRequest(current.request.id, {
        version: current.request.version,
        ...changes,
      });
      detail.value = updated;
      refreshRow(updated);
      toasts.success("Сохранено");
      return true;
    } catch (caught) {
      report(caught, "Не удалось сохранить пожелание");
      if (isConflict(caught)) {
        await open(current.request.id);
        if (detail.value !== null) {
          refreshRow(detail.value);
        }
      }
      return false;
    } finally {
      saving.value = false;
    }
  }

  /** Answer the buyer; a retry of the same text reuses the same key. */
  async function reply(text: string): Promise<boolean> {
    const current = detail.value;
    const trimmed = text.trim();
    if (current === null || trimmed === "") {
      return false;
    }
    const requestId = current.request.id;
    if (pendingReply === null || pendingReply.requestId !== requestId || pendingReply.text !== trimmed) {
      pendingReply = { requestId, text: trimmed, key: newIdempotencyKey() };
    }
    replying.value = true;
    try {
      const result = await api.daily.reply(requestId, trimmed, pendingReply.key);
      pendingReply = null;
      toasts.success(
        result.created ? "Ответ поставлен в очередь Daily-бота" : "Этот ответ уже в очереди",
      );
      await open(requestId);
      return true;
    } catch (caught) {
      report(caught, "Не удалось отправить ответ");
      return false;
    } finally {
      replying.value = false;
    }
  }

  async function sendItem(itemId: number): Promise<void> {
    const current = detail.value;
    if (current === null) {
      return;
    }
    const key = itemKeys.get(itemId) ?? newIdempotencyKey();
    itemKeys.set(itemId, key);
    sendingItemId.value = itemId;
    try {
      const result = await api.daily.sendRequestItem(current.request.id, itemId, key);
      itemKeys.delete(itemId);
      toasts.success(
        result.created ? "Daily-бот пришлёт вложение вам в Telegram" : "Уже отправляется",
      );
    } catch (caught) {
      report(caught, "Не удалось поставить отправку");
    } finally {
      sendingItemId.value = null;
    }
  }

  return {
    ...list,
    detail,
    saving,
    replying,
    sendingItemId,
    open,
    close,
    save,
    reply,
    sendItem,
  };
});
