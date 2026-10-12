/**
 * A server-paged, filtered list: the shape every Daily page shares.
 *
 * Changing a filter goes back to the first page. Loads are numbered, so a slow
 * response that arrives after a newer one never overwrites it.
 */

import { computed, ref } from "vue";
import type { Ref } from "vue";

import type { PageMeta } from "@/api/endpoints";
import { useToastStore } from "@/stores/toasts";

export interface PageQuery {
  limit: number;
  offset: number;
}

export const DAILY_PAGE_SIZES = [20, 50, 100] as const;

export interface PagedListOptions<ItemT, FiltersT extends object> {
  load: (query: PageQuery & FiltersT) => Promise<{ items: ItemT[]; meta: PageMeta }>;
  filters: FiltersT;
  errorText: string;
}

export function usePagedList<ItemT, FiltersT extends object>(
  options: PagedListOptions<ItemT, FiltersT>,
) {
  const toasts = useToastStore();

  const items = ref([]) as Ref<ItemT[]>;
  const total = ref(0);
  const limit = ref<number>(DAILY_PAGE_SIZES[0]);
  const offset = ref(0);
  const filters = ref({ ...options.filters }) as Ref<FiltersT>;
  const loading = ref(false);
  const loaded = ref(false);
  let generation = 0;

  const page = computed(() => Math.floor(offset.value / limit.value) + 1);
  const pages = computed(() => Math.max(1, Math.ceil(total.value / limit.value)));
  const isEmpty = computed(() => loaded.value && items.value.length === 0);

  async function fetchPage(): Promise<void> {
    generation += 1;
    const mine = generation;
    loading.value = true;
    try {
      const result = await options.load({
        limit: limit.value,
        offset: offset.value,
        ...filters.value,
      });
      if (mine !== generation) {
        return;
      }
      items.value = result.items;
      total.value = result.meta.total;
      loaded.value = true;
    } catch (caught) {
      if (mine === generation) {
        toasts.reportError(caught, options.errorText);
      }
    } finally {
      if (mine === generation) {
        loading.value = false;
      }
    }
  }

  async function applyFilters(next: Partial<FiltersT> & { limit?: number }): Promise<void> {
    const { limit: nextLimit, ...rest } = next;
    filters.value = { ...filters.value, ...rest };
    if (nextLimit !== undefined) {
      limit.value = nextLimit;
    }
    offset.value = 0;
    await fetchPage();
  }

  async function goTo(nextOffset: number): Promise<void> {
    offset.value = Math.max(0, nextOffset);
    await fetchPage();
  }

  /** Put a fresh copy of one row in place, without reloading the page. */
  function replace(matches: (item: ItemT) => boolean, next: ItemT): void {
    items.value = items.value.map((item) => (matches(item) ? next : item));
  }

  return {
    items,
    total,
    limit,
    offset,
    filters,
    loading,
    loaded,
    page,
    pages,
    isEmpty,
    fetchPage,
    applyFilters,
    goTo,
    replace,
  };
}
