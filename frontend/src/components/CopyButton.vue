<script setup lang="ts">
/** Copies a value to the clipboard, with a fallback for non-secure contexts. */
import { ref } from "vue";

const props = defineProps<{ value: string; label?: string }>();

const copied = ref(false);

async function copy(): Promise<void> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(props.value);
    } else {
      // Older browsers and plain-HTTP origins have no async clipboard.
      const field = document.createElement("textarea");
      field.value = props.value;
      field.setAttribute("readonly", "");
      field.style.position = "absolute";
      field.style.left = "-9999px";
      document.body.appendChild(field);
      field.select();
      document.execCommand("copy");
      document.body.removeChild(field);
    }
    copied.value = true;
    window.setTimeout(() => (copied.value = false), 1500);
  } catch {
    copied.value = false;
  }
}
</script>

<template>
  <button
    class="small"
    type="button"
    :title="value"
    @click="copy"
  >
    {{ copied ? "Скопировано" : (label ?? "Скопировать") }}
  </button>
</template>
