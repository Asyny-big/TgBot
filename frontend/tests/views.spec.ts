/**
 * View rendering: the tables must show what the API returned, and the support
 * actions must be reachable. Stores are stubbed, so this checks templates only.
 */

import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ProductStoreModule from "@/stores/products";
import type * as PurchaseStoreModule from "@/stores/purchases";

import { makeOverview, makeProduct, makeRecord } from "./helpers";

const productStore = {
  items: [makeProduct()],
  total: 1,
  limit: 20,
  offset: 0,
  page: 1,
  pages: 1,
  loading: false,
  loaded: true,
  isEmpty: false,
  fetchPage: vi.fn(),
  applyFilters: vi.fn(),
  goTo: vi.fn(),
  toggleActive: vi.fn(),
  remove: vi.fn(),
  create: vi.fn(),
  update: vi.fn(),
};

const purchaseStore = {
  items: [makeRecord({ status: "paid" })],
  total: 1,
  limit: 20,
  offset: 0,
  page: 1,
  pages: 1,
  loading: false,
  loaded: true,
  isEmpty: false,
  busyId: null as string | null,
  reports: {} as Record<string, unknown>,
  fetchPage: vi.fn(),
  applyFilters: vi.fn(),
  goTo: vi.fn(),
  verify: vi.fn(),
  resend: vi.fn(),
};

const statsStore = { overview: makeOverview(), loading: false, load: vi.fn() };

// Only the store hook is replaced; the module's constants (page sizes) stay real.
vi.mock("@/stores/products", async () => {
  const actual = await vi.importActual<typeof ProductStoreModule>("@/stores/products");
  return { ...actual, useProductStore: () => productStore };
});
vi.mock("@/stores/purchases", async () => {
  const actual = await vi.importActual<typeof PurchaseStoreModule>("@/stores/purchases");
  return { ...actual, usePurchaseStore: () => purchaseStore };
});
vi.mock("@/stores/stats", () => ({ useStatsStore: () => statsStore }));

const ProductsView = (await import("@/views/ProductsView.vue")).default;
const PurchasesView = (await import("@/views/PurchasesView.vue")).default;
const DashboardView = (await import("@/views/DashboardView.vue")).default;

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
});

describe("ProductsView", () => {
  it("loads the first page and renders the product with its deep link", () => {
    const wrapper = mount(ProductsView);

    expect(productStore.fetchPage).toHaveBeenCalledOnce();
    const row = wrapper.find("tbody tr");
    expect(row.text()).toContain("VIP access");
    expect(row.text()).toContain("vip1");
    expect(row.text()).toContain("150 ⭐");
    expect(row.text()).toContain("5 USDT");
    const copy = wrapper.findAll("button").find((button) => button.text() === "Deep link");
    expect(copy).toBeDefined();
  });

  it("asks for confirmation before deleting", async () => {
    const wrapper = mount(ProductsView);
    const deleteButton = wrapper.findAll("button").find((button) => button.text() === "Удалить");

    await deleteButton?.trigger("click");

    expect(productStore.remove).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain("Точно удалить?");

    const confirm = wrapper
      .findAll("button")
      .find((button) => button.text() === "Точно удалить?");
    await confirm?.trigger("click");
    expect(productStore.remove).toHaveBeenCalledOnce();
  });

  it("opens the create dialog", async () => {
    const wrapper = mount(ProductsView);

    await wrapper.findAll("button").find((button) => button.text() === "Новый товар")?.trigger("click");

    expect(wrapper.find(".modal").exists()).toBe(true);
  });
});

describe("PurchasesView", () => {
  it("renders the purchase and both support actions", () => {
    const wrapper = mount(PurchasesView);

    expect(purchaseStore.fetchPage).toHaveBeenCalledOnce();
    const text = wrapper.find("tbody").text();
    expect(text).toContain("@buyer");
    expect(text).toContain("VIP access");
    expect(text).toContain("Оплачено");
    const labels = wrapper.findAll("button").map((button) => button.text());
    expect(labels).toContain("Проверить платеж");
    expect(labels).toContain("Отправить ссылку");
  });

  it("triggers the manual payment check", async () => {
    const wrapper = mount(PurchasesView);

    await wrapper
      .findAll("button")
      .find((button) => button.text() === "Проверить платеж")
      ?.trigger("click");

    expect(purchaseStore.verify).toHaveBeenCalledOnce();
  });

  it("shows the verification report under the row", () => {
    const record = makeRecord({ status: "delivered" });
    purchaseStore.items = [record];
    purchaseStore.reports = {
      [record.purchase.id]: {
        outcome: "settled_and_delivered",
        provider_state: "paid",
        detail: null,
      },
    };

    const wrapper = mount(PurchasesView);

    expect(wrapper.find(".report").text()).toContain("ссылка отправлена");
    purchaseStore.reports = {};
    purchaseStore.items = [makeRecord({ status: "paid" })];
  });

  it("does not offer re-delivery for an unpaid purchase", () => {
    purchaseStore.items = [makeRecord({ status: "pending" })];

    const wrapper = mount(PurchasesView);
    const resend = wrapper
      .findAll("button")
      .find((button) => button.text() === "Отправить ссылку");

    expect(resend?.attributes("disabled")).toBeDefined();
    purchaseStore.items = [makeRecord({ status: "paid" })];
  });
});

describe("DashboardView", () => {
  it("renders the four revenue windows and the counters", () => {
    const wrapper = mount(DashboardView);

    expect(statsStore.load).toHaveBeenCalledOnce();
    const text = wrapper.text();
    for (const label of ["Сегодня", "Неделя", "Месяц", "Всего"]) {
      expect(text).toContain(label);
    }
    expect(text).toContain("1 продаж");
    expect(text).toContain("⭐ 150");
    expect(text).toContain("💎 5 USDT");
    expect(text).toContain("Покупателей");
  });
});
