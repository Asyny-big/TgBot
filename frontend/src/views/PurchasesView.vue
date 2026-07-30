<script setup lang="ts">
/**
 * Purchase search and the two support actions.
 *
 * "Проверить платеж" calls the idempotent verification endpoint and shows what
 * the check found right in the row — that is the whole workflow for "I paid but
 * got no link".
 */
import { onBeforeUnmount, onMounted, ref } from "vue";

import type { PurchaseRecordResponse, PurchaseStatus } from "@/api/endpoints";
import { PROVIDER_LABELS, formatAmount, formatDateTime, outcomeLabel } from "@/api/format";
import Pagination from "@/components/Pagination.vue";
import StatusBadge from "@/components/StatusBadge.vue";
import { PURCHASE_PAGE_SIZES, usePurchaseStore } from "@/stores/purchases";

const SEARCH_DEBOUNCE_MS = 300;

const STATUS_OPTIONS: { value: PurchaseStatus | ""; label: string }[] = [
  { value: "", label: "Все статусы" },
  { value: "pending", label: "Ожидают оплаты" },
  { value: "paid", label: "Оплачены" },
  { value: "delivered", label: "Выданы" },
  { value: "refunded", label: "Возвраты" },
  { value: "expired", label: "Истекли" },
];

const purchases = usePurchaseStore();

const searchInput = ref("");
const statusFilter = ref<PurchaseStatus | "">("");

let debounce: number | undefined;

onMounted(() => {
  void purchases.fetchPage();
});

onBeforeUnmount(() => {
  if (debounce !== undefined) {
    window.clearTimeout(debounce);
  }
});

function onSearchInput(): void {
  if (debounce !== undefined) {
    window.clearTimeout(debounce);
  }
  debounce = window.setTimeout(() => {
    void purchases.applyFilters({ search: searchInput.value });
  }, SEARCH_DEBOUNCE_MS);
}

function onStatusChange(): void {
  void purchases.applyFilters({ status: statusFilter.value === "" ? null : statusFilter.value });
}

function canResend(record: PurchaseRecordResponse): boolean {
  return record.purchase.status === "paid" || record.purchase.status === "delivered";
}
</script>

<template>
  <section>
    <div class="page-head">
      <h1>Покупки</h1>
      <button
        type="button"
        :disabled="purchases.loading"
        @click="purchases.fetchPage()"
      >
        {{ purchases.loading ? "Обновляю…" : "Обновить" }}
      </button>
    </div>

    <div class="toolbar">
      <input
        v-model="searchInput"
        class="grow"
        type="search"
        placeholder="Telegram ID, username, товар, invoice или transaction"
        @input="onSearchInput"
      >
      <select
        v-model="statusFilter"
        aria-label="Фильтр по статусу"
        @change="onStatusChange"
      >
        <option
          v-for="option in STATUS_OPTIONS"
          :key="option.value"
          :value="option.value"
        >
          {{ option.label }}
        </option>
      </select>
    </div>

    <div class="panel">
      <p
        v-if="purchases.loading && purchases.items.length === 0"
        class="state"
      >
        Загружаю…
      </p>
      <p
        v-else-if="purchases.isEmpty"
        class="state"
      >
        Ничего не найдено
      </p>
      <table v-else>
        <thead>
          <tr>
            <th>Когда</th>
            <th>Покупатель</th>
            <th>Товар</th>
            <th>Способ</th>
            <th>Сумма</th>
            <th>Статус</th>
            <th>Invoice</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <template
            v-for="record in purchases.items"
            :key="record.purchase.id"
          >
            <tr>
              <td>{{ formatDateTime(record.purchase.created_at) }}</td>
              <td>
                <div>{{ record.buyer.display_name }}</div>
                <div class="mono">
                  {{ record.buyer.telegram_id }}
                </div>
              </td>
              <td>
                <div
                  class="truncate"
                  :title="record.product.title"
                >
                  {{ record.product.title }}
                </div>
                <div class="mono">
                  {{ record.product.slug }}
                </div>
              </td>
              <td>{{ PROVIDER_LABELS[record.purchase.provider] ?? record.purchase.provider }}</td>
              <td>{{ formatAmount(record.purchase.amount) }} {{ record.purchase.currency }}</td>
              <td><StatusBadge :status="record.purchase.status" /></td>
              <td>
                <div
                  class="mono truncate"
                  :title="record.purchase.external_id"
                >
                  {{ record.purchase.external_id }}
                </div>
                <div
                  v-if="record.purchase.telegram_charge_id"
                  class="mono truncate"
                >
                  {{ record.purchase.telegram_charge_id }}
                </div>
              </td>
              <td class="actions">
                <button
                  class="small"
                  type="button"
                  :disabled="purchases.busyId === record.purchase.id"
                  @click="purchases.verify(record)"
                >
                  Проверить платеж
                </button>
                <button
                  class="small"
                  type="button"
                  :disabled="purchases.busyId === record.purchase.id || !canResend(record)"
                  :title="canResend(record) ? '' : 'Доступно только для оплаченных покупок'"
                  @click="purchases.resend(record)"
                >
                  Отправить ссылку
                </button>
              </td>
            </tr>
            <tr v-if="purchases.reports[record.purchase.id]">
              <td
                colspan="8"
                class="report"
              >
                <strong>Проверка:</strong>
                {{ outcomeLabel(purchases.reports[record.purchase.id]!.outcome) }}
                <template v-if="purchases.reports[record.purchase.id]!.provider_state">
                  · провайдер: {{ purchases.reports[record.purchase.id]!.provider_state }}
                </template>
                <template v-if="purchases.reports[record.purchase.id]!.detail">
                  · {{ purchases.reports[record.purchase.id]!.detail }}
                </template>
              </td>
            </tr>
          </template>
        </tbody>
      </table>

      <Pagination
        :total="purchases.total"
        :limit="purchases.limit"
        :offset="purchases.offset"
        :page="purchases.page"
        :pages="purchases.pages"
        :page-sizes="PURCHASE_PAGE_SIZES"
        :loading="purchases.loading"
        @go="purchases.goTo($event)"
        @size="purchases.applyFilters({ limit: $event })"
      />
    </div>
  </section>
</template>
