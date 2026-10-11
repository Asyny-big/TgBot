/** Builders of the Daily Content API's responses, for the Daily tests. */

import { vi } from "vitest";

import type {
  DailyContentResponse,
  DailyOverviewResponse,
  DailyRequestDetailResponse,
  DailyRequestResponse,
  DailySubscriberDetailResponse,
  DailySubscriberResponse,
} from "@/api/endpoints";

const AT = "2026-10-11T09:00:00Z";

export function makeDailyContent(
  overrides: Partial<DailyContentResponse> = {},
): DailyContentResponse {
  return {
    id: 12,
    status: "active",
    kind: "video",
    body: "Morning stretch",
    file_name: "stretch.mp4",
    mime_type: "video/mp4",
    file_size: 4_200_000,
    duration: 45,
    width: 1280,
    height: 720,
    added_by: 9000,
    created_at: AT,
    status_changed_at: AT,
    status_reason: null,
    sent_count: 3,
    ...overrides,
  };
}

export function makeDailyRequest(
  overrides: Partial<DailyRequestResponse> = {},
): DailyRequestResponse {
  return {
    id: 7,
    user_id: 4242,
    buyer: { user_id: 4242, username: "kate", first_name: "Kate" },
    status: "new",
    version: 1,
    purchases_at_submit: 21,
    admin_note: null,
    item_count: 2,
    reply_count: 0,
    created_at: AT,
    updated_at: AT,
    closed_at: null,
    ...overrides,
  };
}

export function makeDailyRequestDetail(
  overrides: Partial<DailyRequestResponse> = {},
): DailyRequestDetailResponse {
  return {
    request: makeDailyRequest(overrides),
    items: [
      {
        id: 31,
        position: 1,
        kind: "text",
        body: "More yoga, please",
        file_name: null,
        mime_type: null,
        file_size: null,
        duration: null,
        has_file: false,
        created_at: AT,
      },
      {
        id: 32,
        position: 2,
        kind: "photo",
        body: "like this",
        file_name: null,
        mime_type: null,
        file_size: 98_000,
        duration: null,
        has_file: true,
        created_at: AT,
      },
    ],
    events: [
      {
        id: 1,
        kind: "submitted",
        actor: "user:4242",
        created_at: AT,
        from_status: null,
        to_status: "new",
        body: null,
        outbox_message_id: null,
      },
    ],
    replies: [],
    banned: false,
  };
}

export function makeDailySubscriber(
  overrides: Partial<DailySubscriberResponse> = {},
): DailySubscriberResponse {
  return {
    user_id: 4242,
    username: "kate",
    first_name: "Kate",
    status: "active",
    first_interaction_at: AT,
    activated_at: AT,
    anchor_minute: 755,
    paused_at: null,
    blocked_at: null,
    requests_banned_at: null,
    requests_banned_by: null,
    completed_purchases: 21,
    received: 9,
    last_received_at: AT,
    open_requests: 1,
    can_suggest: true,
    ...overrides,
  };
}

export function makeDailySubscriberDetail(
  overrides: Partial<DailySubscriberResponse> = {},
): DailySubscriberDetailResponse {
  return { subscriber: makeDailySubscriber(overrides), deliveries: [], requests: [] };
}

export function makeDailyOverview(): DailyOverviewResponse {
  return {
    enabled: true,
    today: "2026-10-11",
    subscriptions: { inactive: 2, active: 40, paused: 3, blocked: 1, banned: 1 },
    deliveries_today: {
      pending: 5,
      sending: 0,
      sent: 30,
      no_content: 0,
      failed: 1,
      expired: 0,
      cancelled: 0,
    },
    requests: { new: 4, in_review: 2, completed: 10, rejected: 1 },
    outbox: { open: 2, failed_recently: 0 },
    library: {
      active: 120,
      disabled: 4,
      archived: 9,
      total: 133,
      active_by_kind: [{ kind: "video", count: 120 }],
    },
    alerts: [
      {
        key: "low_stock",
        state: "firing",
        notifications: 1,
        value: { min_days: 2 },
        updated_at: AT,
        fired_at: AT,
        notified_at: AT,
        resolved_at: null,
      },
    ],
  };
}

export function page<ItemT>(items: ItemT[], total = items.length) {
  return { items, meta: { total, limit: 20, offset: 0, has_more: false } };
}

/** The Daily part of the API module, as spies. */
export function stubDailyApi() {
  return {
    overview: vi.fn(),
    alerts: vi.fn(),
    library: vi.fn(),
    changeStatus: vi.fn(),
    sendLibraryItem: vi.fn(),
    subscribers: vi.fn(),
    subscriber: vi.fn(),
    setRequestsBan: vi.fn(),
    deliveries: vi.fn(),
    delivery: vi.fn(),
    requests: vi.fn(),
    request: vi.fn(),
    updateRequest: vi.fn(),
    reply: vi.fn(),
    sendRequestItem: vi.fn(),
  };
}
