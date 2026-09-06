/** Shared test scaffolding: a fresh Pinia and a stubbed API module. */

import { createPinia, setActivePinia } from "pinia";
import { beforeEach, vi } from "vitest";

import type {
  OverviewResponse,
  ProductResponse,
  PurchaseRecordResponse,
  ReferralRecordResponse,
  ReferralSettingsResponse,
} from "@/api/endpoints";

export function usePiniaForEachTest(): void {
  beforeEach(() => {
    setActivePinia(createPinia());
  });
}

export function makeProduct(overrides: Partial<ProductResponse> = {}): ProductResponse {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    slug: "vip1",
    title: "VIP access",
    description: "Lifetime access",
    photo_file_id: null,
    delivery_url: "https://t.me/+invite",
    price_stars: 150,
    price_usdt: "5.00",
    is_active: true,
    deep_link: "https://t.me/MyShopBot?start=vip1",
    created_at: "2026-07-30T10:00:00Z",
    updated_at: "2026-07-30T10:00:00Z",
    ...overrides,
  };
}

export function makeRecord(
  overrides: Partial<PurchaseRecordResponse["purchase"]> = {},
): PurchaseRecordResponse {
  return {
    purchase: {
      id: "22222222-2222-4222-8222-222222222222",
      user_id: 4242,
      product_id: "11111111-1111-4111-8111-111111111111",
      provider: "crypto",
      status: "pending",
      amount: "5.000000",
      currency: "USDT",
      external_id: "770001",
      telegram_charge_id: null,
      delivered_url: null,
      created_at: "2026-07-30T10:00:00Z",
      paid_at: null,
      delivered_at: null,
      ...overrides,
    },
    buyer: {
      telegram_id: 4242,
      username: "buyer",
      first_name: "Buyer",
      display_name: "@buyer",
    },
    product: {
      id: "11111111-1111-4111-8111-111111111111",
      slug: "vip1",
      title: "VIP access",
    },
  };
}

export function makeReferral(
  overrides: Partial<ReferralRecordResponse> = {},
): ReferralRecordResponse {
  return {
    id: "33333333-3333-4333-8333-333333333333",
    referrer: {
      buyer: {
        telegram_id: 7001,
        username: "inviter",
        first_name: "A",
        display_name: "@inviter",
      },
      bonus_balance: 135,
    },
    referred: {
      buyer: {
        telegram_id: 7002,
        username: "invited",
        first_name: "B",
        display_name: "@invited",
      },
      bonus_balance: 0,
    },
    created_at: "2026-07-30T10:00:00Z",
    discount_used_at: null,
    discount_purchase_id: null,
    referred_purchase_count: 1,
    reward_total: 135,
    ...overrides,
  };
}

export function makeReferralSettings(
  overrides: Partial<ReferralSettingsResponse> = {},
): ReferralSettingsResponse {
  return {
    enabled: true,
    discount_percent: 10,
    reward_percent: 15,
    max_bonus_payment_percent: 50,
    preview_directory_url: null,
    ...overrides,
  };
}

export function makeOverview(): OverviewResponse {
  const revenue = (period: OverviewResponse["today"]["period"]) => ({
    period,
    purchases_count: 1,
    stars_amount: 150,
    usdt_amount: "5.000000",
  });
  return {
    today: revenue("today"),
    week: revenue("week"),
    month: revenue("month"),
    total: revenue("all"),
    top_products: [],
    recent_purchases: [],
    products_total: 1,
    products_active: 1,
    users_total: 1,
  };
}

/** Replace the API module with spies; every store test uses this. */
export function stubApi() {
  const stub = {
    auth: {
      login: vi.fn(),
      logout: vi.fn(),
      me: vi.fn(),
    },
    products: {
      list: vi.fn(),
      create: vi.fn(),
      update: vi.fn(),
      remove: vi.fn(),
    },
    purchases: {
      search: vi.fn(),
      verify: vi.fn(),
      resend: vi.fn(),
    },
    referrals: {
      list: vi.fn(),
      settings: vi.fn(),
    },
    stats: {
      overview: vi.fn(),
    },
  };
  return stub;
}
