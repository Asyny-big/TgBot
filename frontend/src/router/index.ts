/**
 * Routing and the auth guard.
 *
 * On a cold load there is no access token in memory, so the guard first tries to
 * resume the session with the refresh cookie; only then does it redirect to the
 * login screen.
 */

import { createRouter, createWebHistory } from "vue-router";

import { useAuthStore } from "@/stores/auth";
import DailyDashboardView from "@/views/DailyDashboardView.vue";
import DailyDeliveriesView from "@/views/DailyDeliveriesView.vue";
import DailyLibraryView from "@/views/DailyLibraryView.vue";
import DailyRequestsView from "@/views/DailyRequestsView.vue";
import DailySubscribersView from "@/views/DailySubscribersView.vue";
import DashboardView from "@/views/DashboardView.vue";
import LoginView from "@/views/LoginView.vue";
import ProductsView from "@/views/ProductsView.vue";
import PurchasesView from "@/views/PurchasesView.vue";
import ReferralsView from "@/views/ReferralsView.vue";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: LoginView, meta: { public: true } },
    { path: "/", name: "dashboard", component: DashboardView },
    { path: "/products", name: "products", component: ProductsView },
    { path: "/purchases", name: "purchases", component: PurchasesView },
    { path: "/referrals", name: "referrals", component: ReferralsView },
    { path: "/daily", name: "daily", component: DailyDashboardView },
    { path: "/daily/library", name: "daily-library", component: DailyLibraryView },
    { path: "/daily/subscribers", name: "daily-subscribers", component: DailySubscribersView },
    { path: "/daily/deliveries", name: "daily-deliveries", component: DailyDeliveriesView },
    { path: "/daily/requests", name: "daily-requests", component: DailyRequestsView },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});

router.beforeEach(async (to) => {
  const auth = useAuthStore();

  if (!auth.isAuthenticated) {
    await auth.restore();
  }

  if (to.meta.public) {
    return auth.isAuthenticated ? { path: "/" } : true;
  }
  if (!auth.isAuthenticated) {
    return { path: "/login", query: to.fullPath === "/" ? {} : { redirect: to.fullPath } };
  }
  return true;
});
