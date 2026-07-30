<script setup lang="ts">
/**
 * Product list.
 *
 * Search is debounced and server-side, so the table works the same with ten
 * products and with ten thousand.
 */
import { onBeforeUnmount, onMounted, ref } from "vue";

import type { ProductCreateRequest, ProductPatchRequest, ProductResponse } from "@/api/endpoints";
import { formatDateTime, formatStars, formatUsdt } from "@/api/format";
import CopyButton from "@/components/CopyButton.vue";
import Pagination from "@/components/Pagination.vue";
import ProductForm from "@/components/ProductForm.vue";
import { PAGE_SIZES, useProductStore } from "@/stores/products";

const SEARCH_DEBOUNCE_MS = 300;

const products = useProductStore();

const searchInput = ref("");
const activeFilter = ref<"all" | "active" | "inactive">("all");
const dialogOpen = ref(false);
const editing = ref<ProductResponse | null>(null);
const saving = ref(false);
const confirmingId = ref<string | null>(null);

let debounce: number | undefined;

onMounted(() => {
  void products.fetchPage();
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
    void products.applyFilters({ search: searchInput.value });
  }, SEARCH_DEBOUNCE_MS);
}

function onFilterChange(): void {
  const map = { all: null, active: true, inactive: false } as const;
  void products.applyFilters({ activeOnly: map[activeFilter.value] });
}

function openCreate(): void {
  editing.value = null;
  dialogOpen.value = true;
}

function openEdit(product: ProductResponse): void {
  editing.value = product;
  dialogOpen.value = true;
}

async function onCreate(payload: ProductCreateRequest): Promise<void> {
  saving.value = true;
  const ok = await products.create(payload);
  saving.value = false;
  if (ok) {
    dialogOpen.value = false;
  }
}

async function onPatch(payload: ProductPatchRequest): Promise<void> {
  if (!editing.value) {
    return;
  }
  saving.value = true;
  const ok = await products.update(editing.value.id, payload);
  saving.value = false;
  if (ok) {
    dialogOpen.value = false;
  }
}

async function onDelete(product: ProductResponse): Promise<void> {
  if (confirmingId.value !== product.id) {
    // Two-step delete: the second click confirms, so nothing is lost by accident.
    confirmingId.value = product.id;
    window.setTimeout(() => {
      if (confirmingId.value === product.id) {
        confirmingId.value = null;
      }
    }, 4000);
    return;
  }
  confirmingId.value = null;
  await products.remove(product);
}
</script>

<template>
  <section>
    <div class="page-head">
      <h1>Товары</h1>
      <button
        type="button"
        class="primary"
        @click="openCreate"
      >
        Новый товар
      </button>
    </div>

    <div class="toolbar">
      <input
        v-model="searchInput"
        class="grow"
        type="search"
        placeholder="Поиск по названию, slug или описанию"
        @input="onSearchInput"
      >
      <select
        v-model="activeFilter"
        aria-label="Фильтр по доступности"
        @change="onFilterChange"
      >
        <option value="all">
          Все
        </option>
        <option value="active">
          Только активные
        </option>
        <option value="inactive">
          Только скрытые
        </option>
      </select>
    </div>

    <div class="panel">
      <p
        v-if="products.loading && products.items.length === 0"
        class="state"
      >
        Загружаю…
      </p>
      <p
        v-else-if="products.isEmpty"
        class="state"
      >
        Ничего не найдено. Создайте товар — покупатели увидят его только по deep link.
      </p>
      <table v-else>
        <thead>
          <tr>
            <th>Название</th>
            <th>Slug</th>
            <th>Stars</th>
            <th>USDT</th>
            <th>Статус</th>
            <th>Создан</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="product in products.items"
            :key="product.id"
          >
            <td>
              <div
                class="truncate"
                :title="product.title"
              >
                {{ product.title }}
              </div>
            </td>
            <td class="mono">
              {{ product.slug }}
            </td>
            <td>{{ formatStars(product.price_stars) }}</td>
            <td>{{ formatUsdt(product.price_usdt) }}</td>
            <td>
              <span
                class="badge"
                :class="product.is_active ? 'ok' : 'muted'"
              >
                {{ product.is_active ? "Активен" : "Скрыт" }}
              </span>
            </td>
            <td>{{ formatDateTime(product.created_at) }}</td>
            <td class="actions">
              <CopyButton
                :value="product.deep_link"
                label="Deep link"
              />
              <button
                class="small"
                type="button"
                @click="openEdit(product)"
              >
                Изменить
              </button>
              <button
                class="small"
                type="button"
                @click="products.toggleActive(product)"
              >
                {{ product.is_active ? "Скрыть" : "Включить" }}
              </button>
              <button
                class="small danger"
                type="button"
                @click="onDelete(product)"
              >
                {{ confirmingId === product.id ? "Точно удалить?" : "Удалить" }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <Pagination
        :total="products.total"
        :limit="products.limit"
        :offset="products.offset"
        :page="products.page"
        :pages="products.pages"
        :page-sizes="PAGE_SIZES"
        :loading="products.loading"
        @go="products.goTo($event)"
        @size="products.applyFilters({ limit: $event })"
      />
    </div>

    <ProductForm
      v-if="dialogOpen"
      :product="editing"
      :saving="saving"
      @create="onCreate"
      @patch="onPatch"
      @close="dialogOpen = false"
    />
  </section>
</template>
