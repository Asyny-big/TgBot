<script setup lang="ts">
/**
 * Daily subscribers: their status, window and numbers; one buyer's latest
 * deliveries and requests; the ban from content requests.
 */
import { onBeforeUnmount, onMounted, ref } from "vue";

import {
  DELIVERY_STATUS_LABELS,
  REQUEST_STATUS_LABELS,
  SUBSCRIPTION_STATUS_LABELS,
  buyerName,
  formatWindow,
  label,
} from "@/api/dailyFormat";
import type { DailySubscriptionStatus } from "@/api/endpoints";
import { formatDateTime } from "@/api/format";
import Pagination from "@/components/Pagination.vue";
import { useDailySubscriberStore } from "@/stores/daily";
import { DAILY_PAGE_SIZES } from "@/stores/paged";

const SEARCH_DEBOUNCE_MS = 300;

type Flag = "" | "blocked" | "banned";

const subscribers = useDailySubscriberStore();

const searchInput = ref("");
const statusFilter = ref<DailySubscriptionStatus | "">("");
const flagFilter = ref<Flag>("");

let debounce: number | undefined;

onMounted(() => {
  void subscribers.fetchPage();
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
    void subscribers.applyFilters({ search: searchInput.value.trim() || undefined });
  }, SEARCH_DEBOUNCE_MS);
}

function onFilterChange(): void {
  void subscribers.applyFilters({
    status: statusFilter.value === "" ? undefined : statusFilter.value,
    blocked: flagFilter.value === "blocked" ? true : undefined,
    banned: flagFilter.value === "banned" ? true : undefined,
  });
}
</script>

<template>
  <section>
    <div class="page-head">
      <h1>Daily: подписчики</h1>
      <button
        type="button"
        :disabled="subscribers.loading"
        @click="subscribers.fetchPage()"
      >
        {{ subscribers.loading ? "Обновляю…" : "Обновить" }}
      </button>
    </div>

    <div class="toolbar">
      <input
        v-model="searchInput"
        class="grow"
        type="search"
        placeholder="Telegram ID или username"
        @input="onSearchInput"
      >
      <select
        v-model="statusFilter"
        aria-label="Статус"
        @change="onFilterChange"
      >
        <option value="">
          Все статусы
        </option>
        <option
          v-for="(text, value) in SUBSCRIPTION_STATUS_LABELS"
          :key="value"
          :value="value"
        >
          {{ text }}
        </option>
      </select>
      <select
        v-model="flagFilter"
        aria-label="Отметки"
        @change="onFilterChange"
      >
        <option value="">
          Все
        </option>
        <option value="blocked">
          Заблокировали бота
        </option>
        <option value="banned">
          Запрет пожеланий
        </option>
      </select>
    </div>

    <div class="panel">
      <p
        v-if="subscribers.loading && subscribers.items.length === 0"
        class="state"
      >
        Загружаю…
      </p>
      <p
        v-else-if="subscribers.isEmpty"
        class="state"
      >
        Ничего не найдено
      </p>
      <table v-else>
        <thead>
          <tr>
            <th>Покупатель</th>
            <th>Статус</th>
            <th>Окно (МСК)</th>
            <th>Покупок</th>
            <th>Получено</th>
            <th>Последний материал</th>
            <th>Пожелания</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in subscribers.items"
            :key="row.user_id"
          >
            <td>
              <div>{{ buyerName({ user_id: row.user_id, username: row.username, first_name: row.first_name }) }}</div>
              <div class="mono">
                {{ row.user_id }}
              </div>
            </td>
            <td>
              {{ label(SUBSCRIPTION_STATUS_LABELS, row.status) }}
              <span
                v-if="row.blocked_at"
                class="badge danger"
              >заблокировал</span>
            </td>
            <td>{{ formatWindow(row.anchor_minute) }}</td>
            <td>{{ row.completed_purchases }}</td>
            <td>{{ row.received }}</td>
            <td>{{ formatDateTime(row.last_received_at) }}</td>
            <td>
              <span v-if="row.requests_banned_at"><span class="badge danger">запрет</span></span>
              <span v-else-if="row.can_suggest">открыты · {{ row.open_requests }} ждут</span>
              <span
                v-else
                class="hint"
              >с 20 покупок</span>
            </td>
            <td class="actions">
              <button
                class="small"
                type="button"
                @click="subscribers.open(row.user_id)"
              >
                Открыть
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <Pagination
        :total="subscribers.total"
        :limit="subscribers.limit"
        :offset="subscribers.offset"
        :page="subscribers.page"
        :pages="subscribers.pages"
        :page-sizes="DAILY_PAGE_SIZES"
        :loading="subscribers.loading"
        @go="subscribers.goTo($event)"
        @size="subscribers.applyFilters({ limit: $event })"
      />
    </div>

    <div
      v-if="subscribers.detail"
      class="panel detail"
    >
      <div class="panel-head detail-head">
        <span>
          {{ buyerName({
            user_id: subscribers.detail.subscriber.user_id,
            username: subscribers.detail.subscriber.username,
            first_name: subscribers.detail.subscriber.first_name,
          }) }}
          · {{ subscribers.detail.subscriber.user_id }}
        </span>
        <button
          class="small ghost"
          type="button"
          @click="subscribers.close()"
        >
          Закрыть
        </button>
      </div>
      <div class="detail-body">
        <p>
          Активирован {{ formatDateTime(subscribers.detail.subscriber.activated_at) }} ·
          окно {{ formatWindow(subscribers.detail.subscriber.anchor_minute) }} МСК ·
          покупок {{ subscribers.detail.subscriber.completed_purchases }}
        </p>
        <p v-if="subscribers.detail.subscriber.requests_banned_at">
          Пожелания запрещены {{ formatDateTime(subscribers.detail.subscriber.requests_banned_at) }}
          ({{ subscribers.detail.subscriber.requests_banned_by }})
        </p>
        <button
          type="button"
          :class="subscribers.detail.subscriber.requests_banned_at ? '' : 'danger'"
          :disabled="subscribers.busy"
          @click="subscribers.setBan(
            subscribers.detail.subscriber.user_id,
            !subscribers.detail.subscriber.requests_banned_at,
          )"
        >
          {{ subscribers.detail.subscriber.requests_banned_at ? "Разрешить пожелания" : "Запретить пожелания" }}
        </button>

        <h2>Последние доставки</h2>
        <p
          v-if="subscribers.detail.deliveries.length === 0"
          class="hint"
        >
          Доставок не было
        </p>
        <table v-else>
          <tbody>
            <tr
              v-for="delivery in subscribers.detail.deliveries"
              :key="delivery.id"
            >
              <td>{{ delivery.delivery_date }}</td>
              <td>{{ label(DELIVERY_STATUS_LABELS, delivery.status) }}</td>
              <td class="mono">
                {{ delivery.content_id ? `#${delivery.content_id}` : "—" }}
              </td>
              <td>{{ formatDateTime(delivery.sent_at) }}</td>
            </tr>
          </tbody>
        </table>

        <h2>Пожелания</h2>
        <p
          v-if="subscribers.detail.requests.length === 0"
          class="hint"
        >
          Пожеланий не было
        </p>
        <table v-else>
          <tbody>
            <tr
              v-for="request in subscribers.detail.requests"
              :key="request.id"
            >
              <td class="mono">
                #{{ request.id }}
              </td>
              <td>{{ label(REQUEST_STATUS_LABELS, request.status) }}</td>
              <td>{{ formatDateTime(request.created_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>
