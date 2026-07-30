<script setup lang="ts">
import { onMounted } from "vue";

import { formatAmount, formatDateTime } from "@/api/format";
import StatusBadge from "@/components/StatusBadge.vue";
import { useStatsStore } from "@/stores/stats";

const stats = useStatsStore();

onMounted(() => {
  void stats.load();
});
</script>

<template>
  <section>
    <div class="page-head">
      <h1>Статистика</h1>
      <button
        type="button"
        :disabled="stats.loading"
        @click="stats.load()"
      >
        {{ stats.loading ? "Обновляю…" : "Обновить" }}
      </button>
    </div>

    <div
      v-if="!stats.overview"
      class="panel"
    >
      <p class="state">
        {{ stats.loading ? "Загружаю…" : "Нет данных" }}
      </p>
    </div>

    <template v-else>
      <div class="cards">
        <div
          v-for="entry in [
            { label: 'Сегодня', data: stats.overview.today },
            { label: 'Неделя', data: stats.overview.week },
            { label: 'Месяц', data: stats.overview.month },
            { label: 'Всего', data: stats.overview.total },
          ]"
          :key="entry.label"
          class="card"
        >
          <div class="label">
            {{ entry.label }}
          </div>
          <div class="value">
            {{ entry.data.purchases_count }} продаж
          </div>
          <div class="sub">
            ⭐ {{ entry.data.stars_amount }} · 💎 {{ formatAmount(entry.data.usdt_amount) }} USDT
          </div>
        </div>
      </div>

      <div class="cards">
        <div class="card">
          <div class="label">
            Товаров
          </div>
          <div class="value">
            {{ stats.overview.products_total }}
          </div>
          <div class="sub">
            активных: {{ stats.overview.products_active }}
          </div>
        </div>
        <div class="card">
          <div class="label">
            Покупателей
          </div>
          <div class="value">
            {{ stats.overview.users_total }}
          </div>
          <div class="sub">
            уникальных Telegram ID
          </div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-head">
          Топ товаров за месяц
        </div>
        <p
          v-if="stats.overview.top_products.length === 0"
          class="state"
        >
          Продаж пока нет
        </p>
        <table v-else>
          <thead>
            <tr>
              <th>Товар</th>
              <th>Slug</th>
              <th>Продаж</th>
              <th>Stars</th>
              <th>USDT</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in stats.overview.top_products"
              :key="item.product_id"
            >
              <td>{{ item.title }}</td>
              <td class="mono">
                {{ item.slug }}
              </td>
              <td>{{ item.purchases_count }}</td>
              <td>{{ item.stars_amount }}</td>
              <td>{{ formatAmount(item.usdt_amount) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="panel">
        <div class="panel-head">
          Последние покупки
        </div>
        <p
          v-if="stats.overview.recent_purchases.length === 0"
          class="state"
        >
          Покупок пока нет
        </p>
        <table v-else>
          <thead>
            <tr>
              <th>Когда</th>
              <th>Покупатель</th>
              <th>Товар</th>
              <th>Сумма</th>
              <th>Статус</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="record in stats.overview.recent_purchases"
              :key="record.purchase.id"
            >
              <td>{{ formatDateTime(record.purchase.paid_at ?? record.purchase.created_at) }}</td>
              <td>{{ record.buyer.display_name }}</td>
              <td>{{ record.product.title }}</td>
              <td>
                {{ formatAmount(record.purchase.amount) }} {{ record.purchase.currency }}
              </td>
              <td><StatusBadge :status="record.purchase.status" /></td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </section>
</template>
