<script setup lang="ts">
/**
 * The Daily Content library: filter, switch items off and on, archive,
 * restore. The panel never shows a file — "📨 Прислать мне" has the Daily bot
 * send the item to the owner's own Telegram, exactly as buyers get it.
 */
import { onBeforeUnmount, onMounted, ref } from "vue";

import {
  CONTENT_KIND_LABELS,
  CONTENT_STATUS_LABELS,
  LIBRARY_ACTIONS,
  LIBRARY_ACTION_LABELS,
  formatBytes,
  formatDuration,
  label,
} from "@/api/dailyFormat";
import type { DailyContentKind, DailyContentStatus } from "@/api/endpoints";
import { formatDateTime } from "@/api/format";
import Pagination from "@/components/Pagination.vue";
import { useDailyLibraryStore } from "@/stores/daily";
import { DAILY_PAGE_SIZES } from "@/stores/paged";

const SEARCH_DEBOUNCE_MS = 300;
const STATUS_TONES: Record<DailyContentStatus, string> = {
  active: "ok",
  disabled: "warn",
  archived: "muted",
};

const library = useDailyLibraryStore();

const searchInput = ref("");
const statusFilter = ref<DailyContentStatus | "">("");
const kindFilter = ref<DailyContentKind | "">("");

let debounce: number | undefined;

onMounted(() => {
  void library.fetchPage();
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
    void library.applyFilters({ search: searchInput.value.trim() || undefined });
  }, SEARCH_DEBOUNCE_MS);
}

function onFilterChange(): void {
  void library.applyFilters({
    status: statusFilter.value === "" ? undefined : statusFilter.value,
    kind: kindFilter.value === "" ? undefined : kindFilter.value,
  });
}
</script>

<template>
  <section>
    <div class="page-head">
      <h1>Daily: библиотека</h1>
      <button
        type="button"
        :disabled="library.loading"
        @click="library.fetchPage()"
      >
        {{ library.loading ? "Обновляю…" : "Обновить" }}
      </button>
    </div>

    <div class="toolbar">
      <input
        v-model="searchInput"
        class="grow"
        type="search"
        placeholder="Номер, текст, подпись или имя файла"
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
          v-for="(text, value) in CONTENT_STATUS_LABELS"
          :key="value"
          :value="value"
        >
          {{ text }}
        </option>
      </select>
      <select
        v-model="kindFilter"
        aria-label="Вид"
        @change="onFilterChange"
      >
        <option value="">
          Все виды
        </option>
        <option
          v-for="(text, value) in CONTENT_KIND_LABELS"
          :key="value"
          :value="value"
        >
          {{ text }}
        </option>
      </select>
    </div>

    <div class="panel">
      <p
        v-if="library.loading && library.items.length === 0"
        class="state"
      >
        Загружаю…
      </p>
      <p
        v-else-if="library.isEmpty"
        class="state"
      >
        Ничего не найдено
      </p>
      <table v-else>
        <thead>
          <tr>
            <th>#</th>
            <th>Вид</th>
            <th>Текст / подпись</th>
            <th>Файл</th>
            <th>Статус</th>
            <th>Получили</th>
            <th>Добавлен</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in library.items"
            :key="item.id"
          >
            <td class="mono">
              #{{ item.id }}
            </td>
            <td>
              {{ label(CONTENT_KIND_LABELS, item.kind) }}
              <span
                v-if="item.duration"
                class="hint"
              >{{ formatDuration(item.duration) }}</span>
            </td>
            <td>
              <div
                class="truncate"
                :title="item.body ?? ''"
              >
                {{ item.body ?? "—" }}
              </div>
            </td>
            <td>
              <div
                v-if="item.file_name"
                class="truncate"
                :title="item.file_name"
              >
                {{ item.file_name }}
              </div>
              <div class="mono">
                {{ formatBytes(item.file_size) }}
              </div>
            </td>
            <td>
              <span
                class="badge"
                :class="STATUS_TONES[item.status]"
                :title="item.status_reason ?? ''"
              >{{ label(CONTENT_STATUS_LABELS, item.status) }}</span>
            </td>
            <td>{{ item.sent_count ?? "—" }}</td>
            <td>{{ formatDateTime(item.created_at) }}</td>
            <td class="actions">
              <button
                v-for="action in LIBRARY_ACTIONS[item.status]"
                :key="action"
                class="small"
                type="button"
                :disabled="library.busyId === item.id"
                @click="library.changeStatus(item, action)"
              >
                {{ label(LIBRARY_ACTION_LABELS, action) }}
              </button>
              <button
                class="small"
                type="button"
                :disabled="library.busyId === item.id"
                title="Daily-бот пришлёт материал вам в Telegram"
                @click="library.sendToMe(item)"
              >
                📨 Прислать мне
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <Pagination
        :total="library.total"
        :limit="library.limit"
        :offset="library.offset"
        :page="library.page"
        :pages="library.pages"
        :page-sizes="DAILY_PAGE_SIZES"
        :loading="library.loading"
        @go="library.goTo($event)"
        @size="library.applyFilters({ limit: $event })"
      />
    </div>
  </section>
</template>
