<script setup lang="ts">
/**
 * Daily Content at a glance: subscribers, today's deliveries (Moscow date),
 * the library, buyers' requests, the outbox and the owner's alerts.
 */
import { computed, onMounted } from "vue";

import { ALERT_LABELS, CONTENT_KIND_LABELS, label } from "@/api/dailyFormat";
import { formatDateTime } from "@/api/format";
import { useDailyOverviewStore } from "@/stores/daily";

const daily = useDailyOverviewStore();

onMounted(() => {
  void daily.load();
});

const firing = computed(() =>
  (daily.overview?.alerts ?? []).filter((alert) => alert.state === "firing").length,
);

function alertValue(value: Record<string, unknown>): string {
  return Object.entries(value)
    .map(([key, item]) => `${key}: ${String(item)}`)
    .join(" · ");
}
</script>

<template>
  <section>
    <div class="page-head">
      <h1>Daily: сводка</h1>
      <button
        type="button"
        :disabled="daily.loading"
        @click="daily.load()"
      >
        {{ daily.loading ? "Обновляю…" : "Обновить" }}
      </button>
    </div>

    <div
      v-if="!daily.overview"
      class="panel"
    >
      <p class="state">
        {{ daily.loading ? "Загружаю…" : "Нет данных" }}
      </p>
    </div>

    <template v-else>
      <div
        v-if="!daily.overview.enabled"
        class="panel"
      >
        <p class="report">
          <strong>Daily выключен.</strong> Ничего не планируется и не отправляется;
          библиотеку и пожелания можно вести как обычно.
        </p>
      </div>

      <div class="cards">
        <div class="card">
          <div class="label">
            Подписчики
          </div>
          <div class="value">
            {{ daily.overview.subscriptions.active }} активных
          </div>
          <div class="sub">
            на паузе {{ daily.overview.subscriptions.paused }} ·
            не активированы {{ daily.overview.subscriptions.inactive }}
          </div>
          <div class="sub">
            заблокировали бота {{ daily.overview.subscriptions.blocked }} ·
            запрет пожеланий {{ daily.overview.subscriptions.banned }}
          </div>
        </div>
        <div class="card">
          <div class="label">
            Доставки за {{ daily.overview.today }} (МСК)
          </div>
          <div class="value">
            {{ daily.overview.deliveries_today.sent }} отправлено
          </div>
          <div class="sub">
            в работе {{ daily.overview.deliveries_today.pending + daily.overview.deliveries_today.sending }} ·
            нет материалов {{ daily.overview.deliveries_today.no_content }}
          </div>
          <div class="sub">
            ошибки {{ daily.overview.deliveries_today.failed }} ·
            просрочено {{ daily.overview.deliveries_today.expired }}
          </div>
        </div>
        <div class="card">
          <div class="label">
            Библиотека
          </div>
          <div class="value">
            {{ daily.overview.library.active }} активных
          </div>
          <div class="sub">
            отключено {{ daily.overview.library.disabled }} ·
            в архиве {{ daily.overview.library.archived }}
          </div>
        </div>
        <div class="card">
          <div class="label">
            Пожелания
          </div>
          <div class="value">
            {{ daily.overview.requests.new }} новых
          </div>
          <div class="sub">
            на рассмотрении {{ daily.overview.requests.in_review }} ·
            выполнено {{ daily.overview.requests.completed }} ·
            отклонено {{ daily.overview.requests.rejected }}
          </div>
        </div>
        <div class="card">
          <div class="label">
            Очередь сообщений
          </div>
          <div class="value">
            {{ daily.overview.outbox.open }} в очереди
          </div>
          <div class="sub">
            не доставлено за сутки {{ daily.overview.outbox.failed_recently }}
          </div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          Активные материалы по видам
        </div>
        <p
          v-if="daily.overview.library.active_by_kind.length === 0"
          class="state"
        >
          Активных материалов нет
        </p>
        <table v-else>
          <tbody>
            <tr
              v-for="entry in daily.overview.library.active_by_kind"
              :key="entry.kind"
            >
              <td>{{ label(CONTENT_KIND_LABELS, entry.kind) }}</td>
              <td>{{ entry.count }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <div class="panel-head">
          Алерты{{ firing ? ` · активных: ${firing}` : "" }}
        </div>
        <p
          v-if="daily.overview.alerts.length === 0"
          class="state"
        >
          Алертов пока не было
        </p>
        <table v-else>
          <thead>
            <tr>
              <th>Алерт</th>
              <th>Состояние</th>
              <th>Последнее наблюдение</th>
              <th>Сработал</th>
              <th>Снят</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="alert in daily.overview.alerts"
              :key="alert.key"
            >
              <td>{{ label(ALERT_LABELS, alert.key) }}</td>
              <td>
                <span
                  class="badge"
                  :class="alert.state === 'firing' ? 'danger' : 'ok'"
                >{{ alert.state === "firing" ? "Активен" : "В норме" }}</span>
              </td>
              <td class="mono">
                {{ alertValue(alert.value) || "—" }}
              </td>
              <td>{{ formatDateTime(alert.fired_at) }}</td>
              <td>{{ formatDateTime(alert.resolved_at) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </section>
</template>
