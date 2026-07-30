<script setup lang="ts">
import { computed } from "vue";

import type { PurchaseStatus } from "@/api/endpoints";
import { statusLabel } from "@/api/format";

const props = defineProps<{ status: PurchaseStatus }>();

const tone = computed(() => {
  switch (props.status) {
    case "delivered":
      return "ok";
    case "paid":
      return "warn";
    case "refunded":
      return "danger";
    default:
      return "muted";
  }
});
</script>

<template>
  <span
    class="badge"
    :class="tone"
  >{{ statusLabel(status) }}</span>
</template>
