<script setup lang="ts">
/** Offset pagination: stays cheap no matter how many rows exist. */
import { computed } from "vue";

const props = defineProps<{
  total: number;
  limit: number;
  offset: number;
  page: number;
  pages: number;
  pageSizes: readonly number[];
  loading?: boolean;
}>();

const emit = defineEmits<{
  (event: "go", offset: number): void;
  (event: "size", limit: number): void;
}>();

const from = computed(() => (props.total === 0 ? 0 : props.offset + 1));
const to = computed(() => Math.min(props.offset + props.limit, props.total));
const canPrev = computed(() => props.offset > 0);
const canNext = computed(() => props.offset + props.limit < props.total);
</script>

<template>
  <div class="pager">
    <span>{{ from }}–{{ to }} из {{ total }}</span>
    <span class="spacer" />
    <label
      for="page-size"
      class="hint"
      style="margin: 0"
    >На странице</label>
    <select
      id="page-size"
      :value="limit"
      :disabled="loading"
      @change="emit('size', Number(($event.target as HTMLSelectElement).value))"
    >
      <option
        v-for="size in pageSizes"
        :key="size"
        :value="size"
      >
        {{ size }}
      </option>
    </select>
    <button
      class="small"
      type="button"
      :disabled="!canPrev || loading"
      @click="emit('go', 0)"
    >
      ⇤
    </button>
    <button
      class="small"
      type="button"
      :disabled="!canPrev || loading"
      @click="emit('go', offset - limit)"
    >
      Назад
    </button>
    <span>{{ page }} / {{ pages }}</span>
    <button
      class="small"
      type="button"
      :disabled="!canNext || loading"
      @click="emit('go', offset + limit)"
    >
      Вперёд
    </button>
  </div>
</template>
