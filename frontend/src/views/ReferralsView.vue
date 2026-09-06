<script setup lang="ts">
/**
 * Referral relationships and bonus balances. Read only.
 *
 * Deliberately not analytics: no funnels, no cohorts, no charts. It answers the
 * two questions an operator actually asks — "who invited whom?" and "what does
 * this person have?" — and reports the percentages the shop is running with.
 */
import { onBeforeUnmount, onMounted, ref } from "vue";

import { formatDateTime } from "@/api/format";
import Pagination from "@/components/Pagination.vue";
import { REFERRAL_PAGE_SIZES, useReferralStore } from "@/stores/referrals";

const SEARCH_DEBOUNCE_MS = 300;

const referrals = useReferralStore();

const searchInput = ref("");

let debounce: number | undefined;

onMounted(() => {
  void referrals.fetchSettings();
  void referrals.fetchPage();
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
    void referrals.applyFilters({ search: searchInput.value });
  }, SEARCH_DEBOUNCE_MS);
}
</script>

<template>
  <section>
    <div class="page-head">
      <h1>Рефералы</h1>
      <button
        type="button"
        :disabled="referrals.loading"
        @click="referrals.fetchPage()"
      >
        {{ referrals.loading ? "Обновляю…" : "Обновить" }}
      </button>
    </div>

    <div
      v-if="referrals.settings"
      class="panel settings"
    >
      <p v-if="!referrals.settings.enabled">
        Реферальная программа <b>выключена</b> — магазин работает как обычно.
      </p>
      <template v-else>
        <span>Скидка приглашённому: <b>{{ referrals.settings.discount_percent }}%</b></span>
        <span>Вознаграждение реферера: <b>{{ referrals.settings.reward_percent }}%</b></span>
        <span>
          Бонусами не более:
          <b>{{ referrals.settings.max_bonus_payment_percent }}%</b> стоимости
        </span>
        <span>1 бонус = 1 ⭐</span>
      </template>
      <p class="hint">
        Значения задаются переменными окружения и меняются деплоем.
      </p>
    </div>

    <div class="toolbar">
      <input
        v-model="searchInput"
        class="grow"
        type="search"
        placeholder="Telegram ID или username любой из сторон"
        @input="onSearchInput"
      >
    </div>

    <div class="panel">
      <p
        v-if="referrals.loading && referrals.items.length === 0"
        class="state"
      >
        Загружаю…
      </p>
      <p
        v-else-if="referrals.isEmpty"
        class="state"
      >
        Ничего не найдено
      </p>
      <table v-else>
        <thead>
          <tr>
            <th>Когда</th>
            <th>Пригласил</th>
            <th>Баланс</th>
            <th>Приглашённый</th>
            <th>Баланс</th>
            <th>Покупок</th>
            <th>Скидка</th>
            <th>Начислено</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="record in referrals.items"
            :key="record.id"
          >
            <td>{{ formatDateTime(record.created_at) }}</td>
            <td>{{ record.referrer.buyer.display_name }}</td>
            <td>{{ record.referrer.bonus_balance }}</td>
            <td>{{ record.referred.buyer.display_name }}</td>
            <td>{{ record.referred.bonus_balance }}</td>
            <td>{{ record.referred_purchase_count }}</td>
            <td>
              <span v-if="record.discount_used_at">
                использована {{ formatDateTime(record.discount_used_at) }}
              </span>
              <span
                v-else
                class="hint"
              >доступна</span>
            </td>
            <td>{{ record.reward_total }}</td>
          </tr>
        </tbody>
      </table>

      <Pagination
        :total="referrals.total"
        :limit="referrals.limit"
        :offset="referrals.offset"
        :page="referrals.page"
        :pages="referrals.pages"
        :page-sizes="REFERRAL_PAGE_SIZES"
        :loading="referrals.loading"
        @go="referrals.goTo($event)"
        @size="referrals.applyFilters({ limit: $event })"
      />
    </div>
  </section>
</template>

<style scoped>
.settings {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem 1.5rem;
  padding: 0.75rem 1rem;
  align-items: baseline;
}

.settings .hint {
  flex-basis: 100%;
}
</style>
