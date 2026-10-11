/**
 * Labels and formatting for the Daily Content pages.
 *
 * Every enum the API returns has a label here, so a value the server adds later
 * still renders (as itself) instead of breaking a page.
 */

import type {
  DailyAlertKey,
  DailyContentKind,
  DailyContentStatus,
  DailyDeliveryStatus,
  DailyLibraryActionName,
  DailyOutboxStatus,
  DailyReplyState,
  DailyRequestEventKind,
  DailyRequestStatus,
  DailySubscriptionStatus,
} from "@/api/endpoints";

export const CONTENT_KIND_LABELS: Record<DailyContentKind, string> = {
  text: "Текст",
  photo: "Фото",
  video: "Видео",
  animation: "GIF",
  document: "Документ",
  audio: "Аудио",
  voice: "Голосовое",
  video_note: "Кружок",
};

export const CONTENT_STATUS_LABELS: Record<DailyContentStatus, string> = {
  active: "Активен",
  disabled: "Отключён",
  archived: "В архиве",
};

export const LIBRARY_ACTION_LABELS: Record<DailyLibraryActionName, string> = {
  disable: "Отключить",
  enable: "Включить",
  archive: "В архив",
  restore: "Восстановить",
};

/** What each status lets the panel do — the server checks it again. */
export const LIBRARY_ACTIONS: Record<DailyContentStatus, DailyLibraryActionName[]> = {
  active: ["disable", "archive"],
  disabled: ["enable", "archive"],
  archived: ["restore"],
};

export const SUBSCRIPTION_STATUS_LABELS: Record<DailySubscriptionStatus, string> = {
  inactive: "Не активирован",
  active: "Активен",
  paused: "На паузе",
};

export const DELIVERY_STATUS_LABELS: Record<DailyDeliveryStatus, string> = {
  pending: "Ожидает",
  sending: "Отправляется",
  sent: "Отправлено",
  no_content: "Нет материалов",
  failed: "Ошибка",
  expired: "Просрочено",
  cancelled: "Отменено",
};

export const REQUEST_STATUS_LABELS: Record<DailyRequestStatus, string> = {
  new: "Новая",
  in_review: "На рассмотрении",
  completed: "Выполнена",
  rejected: "Отклонена",
};

export const REQUEST_EVENT_LABELS: Record<DailyRequestEventKind, string> = {
  submitted: "Отправлена покупателем",
  status_changed: "Статус изменён",
  note_changed: "Заметка изменена",
  reply_queued: "Ответ поставлен в очередь",
  reply_sent: "Ответ доставлен",
  reply_failed: "Ответ не доставлен",
};

export const REPLY_STATE_LABELS: Record<DailyReplyState, string> = {
  queued: "В очереди",
  sent: "Доставлен",
  failed: "Не доставлен",
};

export const OUTBOX_STATUS_LABELS: Record<DailyOutboxStatus, string> = {
  pending: "В очереди",
  sending: "Отправляется",
  sent: "Отправлено",
  failed: "Ошибка",
  expired: "Просрочено",
};

export const ALERT_LABELS: Record<DailyAlertKey, string> = {
  low_stock: "Мало материалов",
  content_exhausted: "Материалы закончились",
  delivery_failures: "Ошибки доставки",
  daily_token_unauthorized: "Токен Daily-бота отклонён",
};

export function label<KeyT extends string>(labels: Record<KeyT, string>, value: KeyT): string {
  return labels[value] ?? value;
}

const WINDOW_HALF_MINUTES = 60;
const MINUTES_PER_DAY = 24 * 60;

function clock(minuteOfDay: number): string {
  const minute = ((minuteOfDay % MINUTES_PER_DAY) + MINUTES_PER_DAY) % MINUTES_PER_DAY;
  const hours = Math.floor(minute / 60);
  return `${String(hours).padStart(2, "0")}:${String(minute % 60).padStart(2, "0")}`;
}

/** The buyer's daily window, Moscow time: `11:35–13:35`. */
export function formatWindow(anchorMinute: number | null | undefined): string {
  if (anchorMinute === null || anchorMinute === undefined) {
    return "—";
  }
  return `${clock(anchorMinute - WINDOW_HALF_MINUTES)}–${clock(anchorMinute + WINDOW_HALF_MINUTES)}`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) {
    return "";
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = String(seconds % 60).padStart(2, "0");
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}` : `${minutes}:${rest}`;
}

const UNITS = ["Б", "КБ", "МБ", "ГБ"];

export function formatBytes(size: number | null | undefined): string {
  if (size === null || size === undefined) {
    return "";
  }
  let value = size;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${UNITS[unit]}`;
}

/** `@username`, else the first name, else the Telegram id. */
export function buyerName(buyer: {
  user_id: number;
  username: string | null;
  first_name: string | null;
} | null, fallbackId?: number): string {
  if (buyer === null) {
    return fallbackId === undefined ? "—" : String(fallbackId);
  }
  if (buyer.username) {
    return `@${buyer.username}`;
  }
  return buyer.first_name ?? String(buyer.user_id);
}

/** A fresh idempotency key for one action of the panel. */
export function newIdempotencyKey(): string {
  return crypto.randomUUID();
}
