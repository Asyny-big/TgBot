<script setup lang="ts">
import { RouterLink, RouterView, useRouter } from "vue-router";

import ToastHost from "@/components/ToastHost.vue";
import { useAuthStore } from "@/stores/auth";

const auth = useAuthStore();
const router = useRouter();

async function signOut(): Promise<void> {
  await auth.logout();
  await router.replace("/login");
}
</script>

<template>
  <div
    v-if="auth.isAuthenticated"
    class="layout"
  >
    <nav class="sidebar">
      <div class="brand">
        Digital Shop
      </div>
      <RouterLink
        to="/"
        active-class="active"
      >
        Статистика
      </RouterLink>
      <RouterLink
        to="/products"
        active-class="active"
      >
        Товары
      </RouterLink>
      <RouterLink
        to="/purchases"
        active-class="active"
      >
        Покупки
      </RouterLink>
      <div class="spacer" />
      <div class="who">
        {{ auth.username }}
      </div>
      <button
        type="button"
        class="ghost"
        @click="signOut"
      >
        Выйти
      </button>
    </nav>
    <main class="content">
      <RouterView />
    </main>
  </div>
  <RouterView v-else />
  <ToastHost />
</template>
