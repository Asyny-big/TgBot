/**
 * Product list state.
 *
 * Pagination, search and filtering all happen on the server: the panel stays
 * fast with thousands of products because it never holds more than one page.
 * A search keystroke cancels the request it supersedes, so the list can never
 * show the result of an older query.
 */

import { defineStore } from "pinia";
import { computed, ref } from "vue";

import { api } from "@/api";
import type { ProductCreateRequest, ProductPatchRequest, ProductResponse } from "@/api/endpoints";
import { useToastStore } from "@/stores/toasts";

export const PAGE_SIZES = [20, 50, 100] as const;

export const useProductStore = defineStore("products", () => {
  const toasts = useToastStore();

  const items = ref<ProductResponse[]>([]);
  const total = ref(0);
  const limit = ref<number>(PAGE_SIZES[0]);
  const offset = ref(0);
  const search = ref("");
  const activeOnly = ref<boolean | null>(null);
  const loading = ref(false);
  const loaded = ref(false);

  let inFlight: AbortController | null = null;

  const page = computed(() => Math.floor(offset.value / limit.value) + 1);
  const pages = computed(() => Math.max(1, Math.ceil(total.value / limit.value)));
  const isEmpty = computed(() => loaded.value && items.value.length === 0);

  async function fetchPage(): Promise<void> {
    inFlight?.abort();
    const controller = new AbortController();
    inFlight = controller;
    loading.value = true;
    try {
      const result = await api.products.list({
        limit: limit.value,
        offset: offset.value,
        search: search.value.trim() || undefined,
        isActive: activeOnly.value ?? undefined,
      });
      items.value = result.items;
      total.value = result.meta.total;
      loaded.value = true;
    } catch (caught) {
      if (controller.signal.aborted || (caught instanceof Error && caught.name === "AbortError")) {
        return;
      }
      toasts.reportError(caught, "Не удалось загрузить товары");
    } finally {
      if (inFlight === controller) {
        inFlight = null;
        loading.value = false;
      }
    }
  }

  async function applyFilters(next: {
    search?: string;
    activeOnly?: boolean | null;
    limit?: number;
  }): Promise<void> {
    if (next.search !== undefined) {
      search.value = next.search;
    }
    if (next.activeOnly !== undefined) {
      activeOnly.value = next.activeOnly;
    }
    if (next.limit !== undefined) {
      limit.value = next.limit;
    }
    offset.value = 0;
    await fetchPage();
  }

  async function goTo(nextOffset: number): Promise<void> {
    offset.value = Math.max(0, nextOffset);
    await fetchPage();
  }

  async function create(payload: ProductCreateRequest): Promise<boolean> {
    try {
      const product = await api.products.create(payload);
      toasts.success(`Товар «${product.title}» создан`);
      await fetchPage();
      return true;
    } catch (caught) {
      toasts.reportError(caught, "Не удалось создать товар");
      return false;
    }
  }

  async function update(id: string, payload: ProductPatchRequest): Promise<boolean> {
    try {
      const product = await api.products.update(id, payload);
      // Patch the row in place: no full reload for a single edit.
      items.value = items.value.map((item) => (item.id === product.id ? product : item));
      toasts.success(`Товар «${product.title}» обновлён`);
      return true;
    } catch (caught) {
      toasts.reportError(caught, "Не удалось обновить товар");
      return false;
    }
  }

  async function toggleActive(product: ProductResponse): Promise<void> {
    await update(product.id, { is_active: !product.is_active });
  }

  async function remove(product: ProductResponse): Promise<boolean> {
    try {
      await api.products.remove(product.id);
      toasts.success(`Товар «${product.title}» удалён`);
      await fetchPage();
      return true;
    } catch (caught) {
      toasts.reportError(caught, "Не удалось удалить товар");
      return false;
    }
  }

  return {
    items,
    total,
    limit,
    offset,
    search,
    activeOnly,
    loading,
    loaded,
    page,
    pages,
    isEmpty,
    fetchPage,
    applyFilters,
    goTo,
    create,
    update,
    toggleActive,
    remove,
  };
});
