<script setup lang="ts">
import { useToastStore } from "@/stores/toasts";

const toasts = useToastStore();
</script>

<template>
  <div
    class="toasts"
    aria-live="polite"
  >
    <div
      v-for="toast in toasts.items"
      :key="toast.id"
      class="toast"
      :class="toast.kind"
    >
      <span>{{ toast.text }}</span>
      <button
        type="button"
        aria-label="Закрыть"
        @click="toasts.dismiss(toast.id)"
      >
        ✕
      </button>
    </div>
  </div>
</template>
