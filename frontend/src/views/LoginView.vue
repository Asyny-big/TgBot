<script setup lang="ts">
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { useAuthStore } from "@/stores/auth";

const auth = useAuthStore();
const router = useRouter();
const route = useRoute();

const username = ref("");
const password = ref("");

async function submit(): Promise<void> {
  const ok = await auth.login(username.value, password.value);
  if (ok) {
    const target = typeof route.query.redirect === "string" ? route.query.redirect : "/";
    await router.replace(target);
  }
}
</script>

<template>
  <div class="login-shell">
    <form
      class="panel login-card"
      @submit.prevent="submit"
    >
      <div class="panel-head">
        Вход в админку
      </div>
      <div class="modal-body">
        <div>
          <label for="login-username">Логин</label>
          <input
            id="login-username"
            v-model.trim="username"
            autocomplete="username"
            autofocus
            required
          >
        </div>
        <div>
          <label for="login-password">Пароль</label>
          <input
            id="login-password"
            v-model="password"
            type="password"
            autocomplete="current-password"
            required
          >
        </div>
        <p
          v-if="auth.error"
          class="error-text"
        >
          {{ auth.error }}
        </p>
      </div>
      <div class="modal-foot">
        <button
          type="submit"
          class="primary"
          :disabled="auth.signingIn"
        >
          {{ auth.signingIn ? "Вхожу…" : "Войти" }}
        </button>
      </div>
    </form>
  </div>
</template>
