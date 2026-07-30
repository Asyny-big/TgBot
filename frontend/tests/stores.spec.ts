/**
 * Store behaviour: filters reset pagination, edits patch rows in place, and the
 * support actions report what happened.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { makeOverview, makeProduct, makeRecord, stubApi, usePiniaForEachTest } from "./helpers";

const apiStub = stubApi();
const sessionExpiredHandlers: (() => void)[] = [];

vi.mock("@/api", async () => {
  const { ApiError } = await import("@/api/client");
  return {
    api: apiStub,
    apiClient: { post: vi.fn(), get: vi.fn() },
    setAccessToken: vi.fn(),
    getAccessToken: vi.fn(),
    onSessionExpired: (handler: () => void) => sessionExpiredHandlers.push(handler),
    ApiError,
  };
});

const { useProductStore } = await import("@/stores/products");
const { usePurchaseStore } = await import("@/stores/purchases");
const { useStatsStore } = await import("@/stores/stats");
const { useToastStore } = await import("@/stores/toasts");

usePiniaForEachTest();

beforeEach(() => {
  vi.clearAllMocks();
});

function productPage(items = [makeProduct()], total = 1) {
  return { items, meta: { total, limit: 20, offset: 0, has_more: false } };
}

function purchasePage(items = [makeRecord()], total = 1) {
  return { items, meta: { total, limit: 20, offset: 0, has_more: false } };
}

describe("product store", () => {
  it("loads a page and exposes pagination", async () => {
    apiStub.products.list.mockResolvedValue(productPage([makeProduct()], 45));
    const store = useProductStore();

    await store.fetchPage();

    expect(apiStub.products.list).toHaveBeenCalledWith({
      limit: 20,
      offset: 0,
      search: undefined,
      isActive: undefined,
    });
    expect(store.total).toBe(45);
    expect(store.pages).toBe(3);
    expect(store.page).toBe(1);
    expect(store.isEmpty).toBe(false);
  });

  it("resets the offset when filters change", async () => {
    apiStub.products.list.mockResolvedValue(productPage());
    const store = useProductStore();
    await store.goTo(40);

    await store.applyFilters({ search: " vip ", activeOnly: true, limit: 50 });

    expect(store.offset).toBe(0);
    expect(apiStub.products.list).toHaveBeenLastCalledWith({
      limit: 50,
      offset: 0,
      search: "vip",
      isActive: true,
    });
  });

  it("patches the edited row in place instead of reloading", async () => {
    apiStub.products.list.mockResolvedValue(productPage());
    const store = useProductStore();
    await store.fetchPage();
    apiStub.products.list.mockClear();
    apiStub.products.update.mockResolvedValue(makeProduct({ title: "Renamed" }));

    const ok = await store.update(makeProduct().id, { title: "Renamed" });

    expect(ok).toBe(true);
    expect(store.items[0]?.title).toBe("Renamed");
    expect(apiStub.products.list).not.toHaveBeenCalled();
  });

  it("toggling activity sends only the flag", async () => {
    apiStub.products.update.mockResolvedValue(makeProduct({ is_active: false }));
    const store = useProductStore();

    await store.toggleActive(makeProduct({ is_active: true }));

    expect(apiStub.products.update).toHaveBeenCalledWith(makeProduct().id, { is_active: false });
  });

  it("reports a failure through the toasts and keeps the list", async () => {
    apiStub.products.list.mockResolvedValue(productPage());
    const store = useProductStore();
    await store.fetchPage();
    apiStub.products.create.mockRejectedValue(new Error("boom"));
    const toasts = useToastStore();

    const ok = await store.create({
      slug: "x",
      title: "x",
      description: "",
      delivery_url: "https://x",
      photo_file_id: null,
      price_stars: 1,
      price_usdt: null,
      is_active: true,
    });

    expect(ok).toBe(false);
    expect(toasts.items.at(-1)?.kind).toBe("error");
    expect(store.items).toHaveLength(1);
  });
});

describe("purchase store", () => {
  it("searches with the status filter", async () => {
    apiStub.purchases.search.mockResolvedValue(purchasePage());
    const store = usePurchaseStore();

    await store.applyFilters({ search: "770001", status: "paid" });

    expect(apiStub.purchases.search).toHaveBeenLastCalledWith({
      limit: 20,
      offset: 0,
      search: "770001",
      status: ["paid"],
    });
  });

  it("verification updates the row, keeps the report and announces success", async () => {
    const record = makeRecord();
    apiStub.purchases.search.mockResolvedValue(purchasePage([record]));
    apiStub.purchases.verify.mockResolvedValue({
      purchase_id: record.purchase.id,
      provider: "crypto",
      outcome: "settled_and_delivered",
      resolved: true,
      status_before: "pending",
      status_after: "delivered",
      provider_state: "paid",
      delivery: { status: "sent", attempts: 1, error: null },
      detail: null,
    });
    const store = usePurchaseStore();
    await store.fetchPage();
    const toasts = useToastStore();

    const report = await store.verify(record);

    expect(report?.outcome).toBe("settled_and_delivered");
    expect(store.items[0]?.purchase.status).toBe("delivered");
    expect(store.reports[record.purchase.id]?.resolved).toBe(true);
    expect(toasts.items.at(-1)?.kind).toBe("success");
    expect(store.busyId).toBeNull();
  });

  it("an unresolved verification is reported as information, not success", async () => {
    const record = makeRecord();
    apiStub.purchases.verify.mockResolvedValue({
      purchase_id: record.purchase.id,
      provider: "crypto",
      outcome: "still_unpaid",
      resolved: false,
      status_before: "pending",
      status_after: "pending",
      provider_state: "pending",
      delivery: null,
      detail: null,
    });
    const store = usePurchaseStore();
    const toasts = useToastStore();

    await store.verify(record);

    expect(toasts.items.at(-1)?.kind).toBe("info");
  });

  it("a failed re-delivery is surfaced as an error", async () => {
    apiStub.purchases.resend.mockResolvedValue({
      status: "failed",
      attempts: 3,
      error: "bot was blocked by the user",
    });
    const store = usePurchaseStore();
    const toasts = useToastStore();

    await store.resend(makeRecord({ status: "paid" }));

    expect(toasts.items.at(-1)).toMatchObject({
      kind: "error",
      text: "bot was blocked by the user",
    });
  });
});

describe("stats store", () => {
  it("loads the overview", async () => {
    apiStub.stats.overview.mockResolvedValue(makeOverview());
    const store = useStatsStore();

    await store.load();

    expect(store.overview?.total.purchases_count).toBe(1);
    expect(store.loading).toBe(false);
  });
});
