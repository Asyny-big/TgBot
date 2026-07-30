<script setup lang="ts">
/**
 * Create / edit dialog.
 *
 * On edit it emits a *patch*: only the fields the administrator actually changed
 * are sent, which is exactly what the API's PATCH semantics expect (and keeps
 * two admins editing different fields from overwriting each other).
 */
import { computed, ref, watch } from "vue";

import type {
  ProductCreateRequest,
  ProductPatchRequest,
  ProductResponse,
} from "@/api/endpoints";

const props = defineProps<{ product: ProductResponse | null; saving: boolean }>();

const emit = defineEmits<{
  (event: "create", payload: ProductCreateRequest): void;
  (event: "patch", payload: ProductPatchRequest): void;
  (event: "close"): void;
}>();

interface FormState {
  slug: string;
  title: string;
  description: string;
  delivery_url: string;
  photo_file_id: string;
  price_stars: string;
  price_usdt: string;
  is_active: boolean;
}

const SLUG_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

function blank(): FormState {
  return {
    slug: "",
    title: "",
    description: "",
    delivery_url: "",
    photo_file_id: "",
    price_stars: "",
    price_usdt: "",
    is_active: true,
  };
}

function fromProduct(product: ProductResponse): FormState {
  return {
    slug: product.slug,
    title: product.title,
    description: product.description,
    delivery_url: product.delivery_url,
    photo_file_id: product.photo_file_id ?? "",
    price_stars: product.price_stars === null ? "" : String(product.price_stars),
    price_usdt: product.price_usdt === null ? "" : String(product.price_usdt),
    is_active: product.is_active,
  };
}

const form = ref<FormState>(props.product ? fromProduct(props.product) : blank());
const localError = ref<string | null>(null);

watch(
  () => props.product,
  (product) => {
    form.value = product ? fromProduct(product) : blank();
    localError.value = null;
  },
);

const isEdit = computed(() => props.product !== null);

function validate(state: FormState): string | null {
  if (!SLUG_PATTERN.test(state.slug)) {
    return "Slug: 1–64 символа из латиницы, цифр, дефиса и подчёркивания.";
  }
  if (state.title.trim().length === 0) {
    return "Название не может быть пустым.";
  }
  if (!/^https?:\/\/.+/.test(state.delivery_url.trim())) {
    return "Ссылка на товар должна начинаться с http:// или https://";
  }
  if (state.price_stars === "" && state.price_usdt === "") {
    return "Задайте хотя бы одну цену — в Stars или в USDT.";
  }
  if (state.price_stars !== "" && !/^\d+$/.test(state.price_stars)) {
    return "Цена в Stars — целое число.";
  }
  if (state.price_usdt !== "" && !/^\d+(\.\d{1,2})?$/.test(state.price_usdt)) {
    return "Цена в USDT — число с не более чем двумя знаками после точки.";
  }
  return null;
}

function submit(): void {
  const state = form.value;
  const problem = validate(state);
  if (problem) {
    localError.value = problem;
    return;
  }
  localError.value = null;

  if (!isEdit.value) {
    emit("create", {
      slug: state.slug.trim(),
      title: state.title.trim(),
      description: state.description,
      delivery_url: state.delivery_url.trim(),
      photo_file_id: state.photo_file_id.trim() || null,
      price_stars: state.price_stars === "" ? null : Number(state.price_stars),
      price_usdt: state.price_usdt === "" ? null : state.price_usdt,
      is_active: state.is_active,
    });
    return;
  }

  const original = props.product as ProductResponse;
  const patch: ProductPatchRequest = {};
  if (state.slug.trim() !== original.slug) {
    patch.slug = state.slug.trim();
  }
  if (state.title.trim() !== original.title) {
    patch.title = state.title.trim();
  }
  if (state.description !== original.description) {
    patch.description = state.description;
  }
  if (state.delivery_url.trim() !== original.delivery_url) {
    patch.delivery_url = state.delivery_url.trim();
  }
  const photo = state.photo_file_id.trim() || null;
  if (photo !== (original.photo_file_id ?? null)) {
    patch.photo_file_id = photo;
  }
  const stars = state.price_stars === "" ? null : Number(state.price_stars);
  if (stars !== original.price_stars) {
    patch.price_stars = stars;
  }
  const usdt = state.price_usdt === "" ? null : state.price_usdt;
  if (usdt !== (original.price_usdt ?? null)) {
    patch.price_usdt = usdt;
  }
  if (state.is_active !== original.is_active) {
    patch.is_active = state.is_active;
  }

  if (Object.keys(patch).length === 0) {
    emit("close");
    return;
  }
  emit("patch", patch);
}
</script>

<template>
  <div
    class="modal-backdrop"
    @click.self="emit('close')"
  >
    <form
      class="modal"
      @submit.prevent="submit"
    >
      <div class="modal-head">
        <span>{{ isEdit ? "Редактировать товар" : "Новый товар" }}</span>
        <button
          type="button"
          class="ghost small"
          aria-label="Закрыть"
          @click="emit('close')"
        >
          ✕
        </button>
      </div>

      <div class="modal-body">
        <div>
          <label for="field-slug">Slug (часть deep link)</label>
          <input
            id="field-slug"
            v-model.trim="form.slug"
            autocomplete="off"
            placeholder="vip1"
          >
          <p class="hint">
            Ссылка будет вида https://t.me/&lt;bot&gt;?start={{ form.slug || "…" }}
          </p>
        </div>

        <div>
          <label for="field-title">Название</label>
          <input
            id="field-title"
            v-model="form.title"
            placeholder="VIP доступ"
          >
        </div>

        <div>
          <label for="field-description">Описание</label>
          <textarea
            id="field-description"
            v-model="form.description"
          />
        </div>

        <div>
          <label for="field-url">Ссылка, которую получит покупатель</label>
          <input
            id="field-url"
            v-model.trim="form.delivery_url"
            placeholder="https://…"
          >
        </div>

        <div class="row-2">
          <div>
            <label for="field-stars">Цена в Stars</label>
            <input
              id="field-stars"
              v-model.trim="form.price_stars"
              inputmode="numeric"
              placeholder="150"
            >
          </div>
          <div>
            <label for="field-usdt">Цена в USDT</label>
            <input
              id="field-usdt"
              v-model.trim="form.price_usdt"
              inputmode="decimal"
              placeholder="5.00"
            >
          </div>
        </div>

        <div>
          <label for="field-photo">Telegram file_id фото (необязательно)</label>
          <input
            id="field-photo"
            v-model.trim="form.photo_file_id"
            autocomplete="off"
          >
        </div>

        <label class="check">
          <input
            v-model="form.is_active"
            type="checkbox"
          >
          Товар доступен к покупке
        </label>

        <p
          v-if="localError"
          class="error-text"
        >
          {{ localError }}
        </p>
      </div>

      <div class="modal-foot">
        <button
          type="button"
          class="ghost"
          @click="emit('close')"
        >
          Отмена
        </button>
        <button
          type="submit"
          class="primary"
          :disabled="saving"
        >
          {{ saving ? "Сохраняю…" : "Сохранить" }}
        </button>
      </div>
    </form>
  </div>
</template>
