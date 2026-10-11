<script setup lang="ts">
/**
 * Buyers' content requests: read one, move it between statuses, keep a note,
 * answer the buyer through the Daily bot.
 *
 * A change names the version on screen; if another tab saved first the server
 * refuses it and the page shows the current state. An answer keeps its
 * idempotency key until the server confirmed it, so "Ответить" pressed again
 * after a failure never sends it twice. Attachments are never shown here:
 * "📨 Прислать мне" has the Daily bot send them to the owner's Telegram.
 */
import { computed, onMounted, ref, watch } from "vue";

import {
  CONTENT_KIND_LABELS,
  OUTBOX_STATUS_LABELS,
  REPLY_STATE_LABELS,
  REQUEST_EVENT_LABELS,
  REQUEST_STATUS_LABELS,
  buyerName,
  formatBytes,
  formatDuration,
  label,
} from "@/api/dailyFormat";
import type { DailyRequestStatus } from "@/api/endpoints";
import { formatDateTime } from "@/api/format";
import Pagination from "@/components/Pagination.vue";
import { useDailyRequestStore } from "@/stores/daily";
import { DAILY_PAGE_SIZES } from "@/stores/paged";

const REPLY_LIMIT = 3500;
const NOTE_LIMIT = 2000;
const STATUS_TONES: Record<DailyRequestStatus, string> = {
  new: "warn",
  in_review: "warn",
  completed: "ok",
  rejected: "muted",
};

const requests = useDailyRequestStore();

const statusFilter = ref<DailyRequestStatus | "">("new");
const statusDraft = ref<DailyRequestStatus>("new");
const noteDraft = ref("");
const replyDraft = ref("");

onMounted(() => {
  void requests.applyFilters({ status: "new" });
});

/** The form follows what the server says the request is now. */
watch(
  () => requests.detail?.request,
  (request) => {
    statusDraft.value = request?.status ?? "new";
    noteDraft.value = request?.admin_note ?? "";
  },
  { immediate: true },
);

const changed = computed(() => {
  const request = requests.detail?.request;
  if (!request) {
    return false;
  }
  return statusDraft.value !== request.status || noteDraft.value.trim() !== (request.admin_note ?? "");
});

function onFilterChange(): void {
  void requests.applyFilters({ status: statusFilter.value === "" ? undefined : statusFilter.value });
}

async function save(): Promise<void> {
  const request = requests.detail?.request;
  if (!request) {
    return;
  }
  const note = noteDraft.value.trim();
  await requests.save({
    ...(statusDraft.value !== request.status ? { status: statusDraft.value } : {}),
    ...(note !== (request.admin_note ?? "") ? { admin_note: note === "" ? null : note } : {}),
  });
}

async function sendReply(): Promise<void> {
  if (await requests.reply(replyDraft.value)) {
    replyDraft.value = "";
  }
}
</script>

<template>
  <section>
    <div class="page-head">
      <h1>Daily: пожелания</h1>
      <button
        type="button"
        :disabled="requests.loading"
        @click="requests.fetchPage()"
      >
        {{ requests.loading ? "Обновляю…" : "Обновить" }}
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
          v-for="(text, value) in REQUEST_STATUS_LABELS"
          :key="value"
          :value="value"
        >
          {{ text }}
        </option>
      </select>
    </div>

    <div class="panel">
      <p
        v-if="requests.loading && requests.items.length === 0"
        class="state"
      >
        Загружаю…
      </p>
      <p
        v-else-if="requests.isEmpty"
        class="state"
      >
        Пожеланий нет
      </p>
      <table v-else>
        <thead>
          <tr>
            <th>#</th>
            <th>Покупатель</th>
            <th>Статус</th>
            <th>Сообщений</th>
            <th>Ответов</th>
            <th>Получено</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in requests.items"
            :key="row.id"
          >
            <td class="mono">
              #{{ row.id }}
            </td>
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
              >{{ label(REQUEST_STATUS_LABELS, row.status) }}</span>
            </td>
            <td>{{ row.item_count ?? "—" }}</td>
            <td>{{ row.reply_count ?? "—" }}</td>
            <td>{{ formatDateTime(row.created_at) }}</td>
            <td class="actions">
              <button
                class="small"
                type="button"
                @click="requests.open(row.id)"
              >
                Открыть
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <Pagination
        :total="requests.total"
        :limit="requests.limit"
        :offset="requests.offset"
        :page="requests.page"
        :pages="requests.pages"
        :page-sizes="DAILY_PAGE_SIZES"
        :loading="requests.loading"
        @go="requests.goTo($event)"
        @size="requests.applyFilters({ limit: $event })"
      />
    </div>

    <div
      v-if="requests.detail"
      class="panel detail"
    >
      <div class="panel-head detail-head">
        <span>
          Пожелание #{{ requests.detail.request.id }} ·
          {{ buyerName(requests.detail.request.buyer, requests.detail.request.user_id) }} ·
          покупок при отправке: {{ requests.detail.request.purchases_at_submit }}
          <span
            v-if="requests.detail.banned"
            class="badge danger"
          >запрет пожеланий</span>
        </span>
        <button
          class="small ghost"
          type="button"
          @click="requests.close()"
        >
          Закрыть
        </button>
      </div>

      <div class="detail-body">
        <h2>Сообщения</h2>
        <ol class="items">
          <li
            v-for="item in requests.detail.items"
            :key="item.id"
          >
            <div v-if="item.kind === 'text'">
              {{ item.body }}
            </div>
            <template v-else>
              <div>
                {{ label(CONTENT_KIND_LABELS, item.kind) }}
                <span class="hint">
                  {{ item.file_name ?? "" }} {{ formatBytes(item.file_size) }}
                  {{ formatDuration(item.duration) }}
                </span>
                <button
                  v-if="item.has_file"
                  class="small"
                  type="button"
                  :disabled="requests.sendingItemId === item.id"
                  title="Daily-бот пришлёт вложение вам в Telegram"
                  @click="requests.sendItem(item.id)"
                >
                  📨 Прислать мне
                </button>
              </div>
              <div v-if="item.body">
                {{ item.body }}
              </div>
            </template>
          </li>
        </ol>

        <h2>Статус и заметка</h2>
        <div class="row-2">
          <div>
            <label for="request-status">Статус</label>
            <select
              id="request-status"
              v-model="statusDraft"
            >
              <option
                v-for="(text, value) in REQUEST_STATUS_LABELS"
                :key="value"
                :value="value"
              >
                {{ text }}
              </option>
            </select>
          </div>
          <div>
            <label for="request-note">Заметка (видна только в админке)</label>
            <textarea
              id="request-note"
              v-model="noteDraft"
              :maxlength="NOTE_LIMIT"
            />
          </div>
        </div>
        <div>
          <button
            class="primary"
            type="button"
            :disabled="!changed || requests.saving"
            @click="save"
          >
            {{ requests.saving ? "Сохраняю…" : "Сохранить" }}
          </button>
          <span class="hint"> версия {{ requests.detail.request.version }}</span>
        </div>

        <h2>Ответ покупателю</h2>
        <ul
          v-if="requests.detail.replies.length > 0"
          class="items"
        >
          <li
            v-for="reply in requests.detail.replies"
            :key="reply.event.id"
          >
            <span
              class="badge"
              :class="reply.state === 'sent' ? 'ok' : reply.state === 'failed' ? 'danger' : 'warn'"
            >{{ label(REPLY_STATE_LABELS, reply.state) }}</span>
            {{ reply.event.body }}
            <span class="hint">
              · {{ reply.event.actor }} · {{ formatDateTime(reply.event.created_at) }}
              <template v-if="reply.message"> · {{ label(OUTBOX_STATUS_LABELS, reply.message.status) }}</template>
              <template v-if="reply.outcome?.body"> · {{ reply.outcome.body }}</template>
            </span>
          </li>
        </ul>
        <textarea
          v-model="replyDraft"
          aria-label="Текст ответа"
          :maxlength="REPLY_LIMIT"
          placeholder="Ответ уйдёт покупателю от Daily-бота"
        />
        <div>
          <button
            class="primary"
            type="button"
            :disabled="replyDraft.trim() === '' || requests.replying"
            @click="sendReply"
          >
            {{ requests.replying ? "Отправляю…" : "Ответить" }}
          </button>
          <span class="hint"> {{ replyDraft.length }}/{{ REPLY_LIMIT }}</span>
        </div>

        <h2>История</h2>
        <ul class="items">
          <li
            v-for="event in requests.detail.events"
            :key="event.id"
          >
            {{ formatDateTime(event.created_at) }} ·
            {{ label(REQUEST_EVENT_LABELS, event.kind) }}
            <template v-if="event.to_status">
              → {{ label(REQUEST_STATUS_LABELS, event.to_status) }}
            </template>
            <span class="hint"> · {{ event.actor }}</span>
          </li>
        </ul>
      </div>
    </div>
  </section>
</template>
