<script setup lang="ts">
/** Daily deliveries: one row per buyer and Moscow day, with every attempt. */
import { onMounted, ref } from "vue";

import { DELIVERY_STATUS_LABELS, buyerName, label } from "@/api/dailyFormat";
import type { DailyDeliveryStatus } from "@/api/endpoints";
import { formatDateTime } from "@/api/format";
import Pagination from "@/components/Pagination.vue";
import { useDailyDeliveryStore } from "@/stores/daily";
import { DAILY_PAGE_SIZES } from "@/stores/paged";

const deliveries = useDailyDeliveryStore();

const statusFilter = ref<DailyDeliveryStatus | "">("");
const dateFilter = ref("");
const userFilter = ref("");

onMounted(() => {
  void deliveries.fetchPage();
});

function onFilterChange(): void {
  const userId = Number.parseInt(userFilter.value, 10);
  void deliveries.applyFilters({
    status: statusFilter.value === "" ? undefined : statusFilter.value,
    date: dateFilter.value || undefined,
    userId: Number.isNaN(userId) ? undefined : userId,
  });
}

const STATUS_TONES: Record<DailyDeliveryStatus, string> = {
  pending: "muted",
  sending: "warn",
  sent: "ok",
  no_content: "warn",
  failed: "danger",
  expired: "danger",
  cancelled: "muted",
};
</script>

<template>
  <section>
    <div class="page-head">
      <h1>Daily: доставки</h1>
      <button
        type="button"
        :disabled="deliveries.loading"
        @click="deliveries.fetchPage()"
      >
        {{ deliveries.loading ? "Обновляю…" : "Обновить" }}
      </button>
    </div>

    <div class="toolbar">
      <select
        v-model="statusFilter"
        aria-label="Статус"
        @change="onFilterChange"
      >
        <option value="">
          Все статусы
        </option>
        <option
          v-for="(text, value) in DELIVERY_STATUS_LABELS"
          :key="value"
          :value="value"
        >
          {{ text }}
        </option>
      </select>
      <input
        v-model="dateFilter"
        type="date"
        aria-label="Дата (МСК)"
        @change="onFilterChange"
      >
      <input
        v-model="userFilter"
        type="search"
        inputmode="numeric"
        placeholder="Telegram ID"
        aria-label="Telegram ID"
        @change="onFilterChange"
      >
    </div>

    <div class="panel">
      <p
        v-if="deliveries.loading && deliveries.items.length === 0"
        class="state"
      >
        Загружаю…
      </p>
      <p
        v-else-if="deliveries.isEmpty"
        class="state"
      >
        Ничего не найдено
      </p>
      <table v-else>
        <thead>
          <tr>
            <th>Дата (МСК)</th>
            <th>Покупатель</th>
            <th>Статус</th>
            <th>Попыток</th>
            <th>Материал</th>
            <th>Отправлено</th>
            <th>Ошибка</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in deliveries.items"
            :key="row.id"
          >
            <td>{{ row.delivery_date }}</td>
            <td>
              <div>{{ buyerName(row.buyer, row.user_id) }}</div>
              <div class="mono">
                {{ row.user_id }}
              </div>
            </td>
            <td>
              <span
                class="badge"
                :class="STATUS_TONES[row.status]"
              >{{ label(DELIVERY_STATUS_LABELS, row.status) }}</span>
            </td>
            <td>{{ row.attempts }}</td>
            <td class="mono">
              {{ row.content_id ? `#${row.content_id}` : "—" }}
            </td>
            <td>{{ formatDateTime(row.sent_at) }}</td>
            <td>
              <div
                class="truncate"
                :title="row.last_error ?? ''"
              >
                {{ row.last_error ?? "" }}
              </div>
            </td>
            <td class="actions">
              <button
                class="small"
                type="button"
                @click="deliveries.open(row.id)"
              >
                Попытки
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <Pagination
        :total="deliveries.total"
        :limit="deliveries.limit"
        :offset="deliveries.offset"
        :page="deliveries.page"
        :pages="deliveries.pages"
        :page-sizes="DAILY_PAGE_SIZES"
        :loading="deliveries.loading"
        @go="deliveries.goTo($event)"
        @size="deliveries.applyFilters({ limit: $event })"
      />
    </div>

    <div
      v-if="deliveries.detail"
      class="panel detail"
    >
      <div class="panel-head detail-head">
        <span>
          Доставка #{{ deliveries.detail.delivery.id }} ·
          {{ deliveries.detail.delivery.delivery_date }} ·
          окно {{ formatDateTime(deliveries.detail.delivery.window_opens_at) }} –
          {{ formatDateTime(deliveries.detail.delivery.window_closes_at) }}
        </span>
        <button
          class="small ghost"
          type="button"
          @click="deliveries.close()"
        >
          Закрыть
        </button>
      </div>
      <div class="detail-body">
        <p v-if="deliveries.detail.item">
          Материал #{{ deliveries.detail.item.id }} · {{ deliveries.detail.item.kind }}
          <span v-if="deliveries.detail.item.body"> · {{ deliveries.detail.item.body }}</span>
        </p>
        <p
          v-if="deliveries.detail.attempts.length === 0"
          class="hint"
        >
          Попыток не было
        </p>
        <table v-else>
          <thead>
            <tr>
              <th>№</th>
              <th>Начата</th>
              <th>Закончена</th>
              <th>Итог</th>
              <th>Ошибка</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="attempt in deliveries.detail.attempts"
              :key="attempt.id"
            >
              <td>{{ attempt.attempt_no }}</td>
              <td>{{ formatDateTime(attempt.started_at) }}</td>
              <td>{{ formatDateTime(attempt.finished_at) }}</td>
              <td>{{ attempt.outcome ?? "в работе" }}</td>
              <td>{{ attempt.error ?? "" }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>
