/**
 * Presentation helpers shared by the views.
 *
 * Money never goes through `Number`: amounts arrive as decimal strings and are
 * trimmed as strings, so a price can never be rounded by a float.
 */

import type { PurchaseStatus, VerificationOutcome } from "@/api/endpoints";

const DATE_FORMAT = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "—";
  }
  return DATE_FORMAT.format(parsed);
}

/** Trim trailing zeros of a decimal string without touching its precision. */
export function formatAmount(value: string | number | null | undefined): string {
  if (value === null || value === undefined) {
    return "—";
  }
  const text = String(value);
  if (!text.includes(".")) {
    return text;
  }
  const trimmed = text.replace(/0+$/, "").replace(/\.$/, "");
  return trimmed === "" ? "0" : trimmed;
}

export function formatStars(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${value} ⭐`;
}

export function formatUsdt(value: string | null | undefined): string {
  return value === null || value === undefined ? "—" : `${formatAmount(value)} USDT`;
}

const STATUS_LABELS: Record<PurchaseStatus, string> = {
  pending: "Ожидает оплаты",
  paid: "Оплачено",
  delivered: "Выдано",
  refunded: "Возврат",
  expired: "Истёк",
};

export function statusLabel(status: PurchaseStatus): string {
  return STATUS_LABELS[status] ?? status;
}

const OUTCOME_LABELS: Record<VerificationOutcome, string> = {
  already_delivered: "Ссылка уже была выдана — ничего делать не нужно.",
  delivered_now: "Оплата была на месте, ссылка отправлена сейчас.",
  settled_and_delivered: "Провайдер подтвердил оплату, ссылка отправлена.",
  delivery_failed: "Оплата подтверждена, но отправить ссылку не удалось.",
  still_unpaid: "Провайдер сообщает, что счёт не оплачен.",
  expired_unpaid: "Счёт истёк и не был оплачен.",
  no_provider_evidence: "Данных об оплате нет: платёж до бота не дошёл.",
  refunded: "По покупке был возврат, доступ отозван.",
  provider_unavailable: "Платёжный провайдер недоступен, попробуйте позже.",
};

export function outcomeLabel(outcome: VerificationOutcome): string {
  return OUTCOME_LABELS[outcome] ?? outcome;
}

export const PROVIDER_LABELS: Record<string, string> = {
  stars: "⭐ Stars",
  crypto: "💎 CryptoBot",
};
