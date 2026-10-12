/**
 * Typed endpoint wrappers.
 *
 * Every request and response type comes from `schema.d.ts`, which is generated
 * from the backend's OpenAPI document (`npm run api:types`). Renaming a field on
 * the server therefore breaks the type check here instead of failing silently in
 * the browser.
 */

import type { components } from "@/api/schema";

import type { ApiClient } from "@/api/client";

type Schemas = components["schemas"];

export type TokenResponse = Schemas["TokenResponse"];
export type AdminResponse = Schemas["AdminResponse"];
export type ProductResponse = Schemas["ProductResponse"];
export type ProductCreateRequest = Schemas["ProductCreateRequest"];
export type ProductPatchRequest = Schemas["ProductPatchRequest"];
export type PurchaseRecordResponse = Schemas["PurchaseRecordResponse"];
export type PurchaseResponse = Schemas["PurchaseResponse"];
export type VerificationResponse = Schemas["VerificationResponse"];
export type DeliveryAttemptResponse = Schemas["DeliveryAttemptResponse"];
export type OverviewResponse = Schemas["OverviewResponse"];
export type ReferralRecordResponse = Schemas["ReferralRecordResponse"];
export type ReferralPartyResponse = Schemas["ReferralPartyResponse"];
export type ReferralSettingsResponse = Schemas["ReferralSettingsResponse"];
export type PageMeta = Schemas["PageMeta"];
export type PurchaseStatus = Schemas["PurchaseStatus"];
export type PaymentProvider = Schemas["PaymentProvider"];
export type VerificationOutcome = Schemas["VerificationOutcome"];

export type ProductPage = Schemas["PageResponse_ProductResponse_"];
export type PurchasePage = Schemas["PageResponse_PurchaseRecordResponse_"];
export type ReferralPage = Schemas["PageResponse_ReferralRecordResponse_"];

export type DailyOverviewResponse = Schemas["DailyOverviewResponse"];
export type DailyAlertResponse = Schemas["DailyAlertResponse"];
export type DailyAlertKey = Schemas["DailyAlertKey"];
export type DailyContentResponse = Schemas["DailyContentResponse"];
export type DailyContentKind = Schemas["DailyContentKind"];
export type DailyContentStatus = Schemas["DailyContentStatus"];
export type DailyLibraryActionName = Schemas["DailyLibraryActionName"];
export type DailySendToOwnerResponse = Schemas["DailySendToOwnerResponse"];
export type DailyOutboxMessageResponse = Schemas["DailyOutboxMessageResponse"];
export type DailyOutboxStatus = Schemas["DailyOutboxStatus"];
export type DailySubscriberResponse = Schemas["DailySubscriberResponse"];
export type DailySubscriberDetailResponse = Schemas["DailySubscriberDetailResponse"];
export type DailySubscriptionStatus = Schemas["DailySubscriptionStatus"];
export type DailyDeliveryResponse = Schemas["DailyDeliveryResponse"];
export type DailyDeliveryDetailResponse = Schemas["DailyDeliveryDetailResponse"];
export type DailyDeliveryStatus = Schemas["DailyDeliveryStatus"];
export type DailyRequestResponse = Schemas["DailyRequestResponse"];
export type DailyRequestDetailResponse = Schemas["DailyRequestDetailResponse"];
export type DailyRequestItemResponse = Schemas["DailyRequestItemResponse"];
export type DailyRequestEventResponse = Schemas["DailyRequestEventResponse"];
export type DailyRequestEventKind = Schemas["DailyRequestEventKind"];
export type DailyRequestStatus = Schemas["DailyRequestStatus"];
export type DailyRequestPatchRequest = Schemas["DailyRequestPatchRequest"];
export type DailyReplyResponse = Schemas["DailyReplyResponse"];
export type DailyReplyResultResponse = Schemas["DailyReplyResultResponse"];
export type DailyReplyState = Schemas["DailyReplyState"];

export type DailyLibraryPage = Schemas["PageResponse_DailyContentResponse_"];
export type DailySubscriberPage = Schemas["PageResponse_DailySubscriberResponse_"];
export type DailyDeliveryPage = Schemas["PageResponse_DailyDeliveryResponse_"];
export type DailyRequestPage = Schemas["PageResponse_DailyRequestResponse_"];

export interface ProductQuery {
  limit: number;
  offset: number;
  search?: string | undefined;
  isActive?: boolean | undefined;
}

export interface PurchaseQuery {
  limit: number;
  offset: number;
  search?: string | undefined;
  status?: PurchaseStatus[] | undefined;
}

export interface ReferralQuery {
  limit: number;
  offset: number;
  search?: string | undefined;
}

export interface DailyLibraryQuery {
  limit: number;
  offset: number;
  status?: DailyContentStatus | undefined;
  kind?: DailyContentKind | undefined;
  search?: string | undefined;
}

export interface DailySubscriberQuery {
  limit: number;
  offset: number;
  status?: DailySubscriptionStatus | undefined;
  blocked?: boolean | undefined;
  banned?: boolean | undefined;
  search?: string | undefined;
}

export interface DailyDeliveryQuery {
  limit: number;
  offset: number;
  status?: DailyDeliveryStatus | undefined;
  userId?: number | undefined;
  date?: string | undefined;
}

export interface DailyRequestQuery {
  limit: number;
  offset: number;
  status?: DailyRequestStatus | undefined;
  userId?: number | undefined;
}

export function createApi(client: ApiClient) {
  return {
    auth: {
      login: (username: string, password: string) =>
        client.post<TokenResponse>("/auth/login", { username, password }),
      logout: () => client.post<null>("/auth/logout", {}),
      me: () => client.get<AdminResponse>("/auth/me"),
    },

    products: {
      list: (query: ProductQuery) =>
        client.get<ProductPage>("/products", {
          query: {
            limit: query.limit,
            offset: query.offset,
            search: query.search,
            is_active: query.isActive,
          },
        }),
      create: (payload: ProductCreateRequest) =>
        client.post<ProductResponse>("/products", payload),
      update: (id: string, payload: ProductPatchRequest) =>
        client.patch<ProductResponse>(`/products/${id}`, payload),
      remove: (id: string) => client.delete<null>(`/products/${id}`),
    },

    purchases: {
      search: (query: PurchaseQuery) =>
        client.get<PurchasePage>("/purchases", {
          query: {
            limit: query.limit,
            offset: query.offset,
            search: query.search,
            status: query.status,
          },
        }),
      verify: (id: string) => client.post<VerificationResponse>(`/purchases/${id}/verify`, {}),
      resend: (id: string) => client.post<DeliveryAttemptResponse>(`/purchases/${id}/resend`, {}),
    },

    /**
     * Read only by design: the referral percentages are process configuration,
     * so the panel reports them and there is nothing here to PATCH.
     */
    referrals: {
      list: (query: ReferralQuery) =>
        client.get<ReferralPage>("/referrals", {
          query: {
            limit: query.limit,
            offset: query.offset,
            search: query.search,
          },
        }),
      settings: () => client.get<ReferralSettingsResponse>("/referrals/settings"),
    },

    stats: {
      overview: () => client.get<OverviewResponse>("/stats/overview"),
    },

    /**
     * Daily Content. Nothing here ever carries a file: "send it to me" has the
     * Daily bot deliver the item to the owner's own Telegram instead. Every
     * write that may be retried carries its own idempotency key.
     */
    daily: {
      overview: () => client.get<DailyOverviewResponse>("/daily/overview"),
      alerts: () => client.get<DailyAlertResponse[]>("/daily/alerts"),
      library: (query: DailyLibraryQuery) =>
        client.get<DailyLibraryPage>("/daily/library", {
          query: {
            limit: query.limit,
            offset: query.offset,
            status: query.status,
            kind: query.kind,
            search: query.search,
          },
        }),
      changeStatus: (contentId: number, action: DailyLibraryActionName) =>
        client.post<DailyContentResponse>(`/daily/library/${contentId}/status`, { action }),
      sendLibraryItem: (contentId: number, idempotencyKey: string) =>
        client.post<DailySendToOwnerResponse>(`/daily/library/${contentId}/send-to-me`, {
          idempotency_key: idempotencyKey,
        }),
      subscribers: (query: DailySubscriberQuery) =>
        client.get<DailySubscriberPage>("/daily/subscribers", {
          query: {
            limit: query.limit,
            offset: query.offset,
            status: query.status,
            blocked: query.blocked,
            banned: query.banned,
            search: query.search,
          },
        }),
      subscriber: (userId: number) =>
        client.get<DailySubscriberDetailResponse>(`/daily/subscribers/${userId}`),
      setRequestsBan: (userId: number, banned: boolean) =>
        client.put<DailySubscriberDetailResponse>(`/daily/subscribers/${userId}/requests-ban`, {
          banned,
        }),
      deliveries: (query: DailyDeliveryQuery) =>
        client.get<DailyDeliveryPage>("/daily/deliveries", {
          query: {
            limit: query.limit,
            offset: query.offset,
            status: query.status,
            user_id: query.userId,
            date: query.date,
          },
        }),
      delivery: (deliveryId: number) =>
        client.get<DailyDeliveryDetailResponse>(`/daily/deliveries/${deliveryId}`),
      requests: (query: DailyRequestQuery) =>
        client.get<DailyRequestPage>("/daily/requests", {
          query: {
            limit: query.limit,
            offset: query.offset,
            status: query.status,
            user_id: query.userId,
          },
        }),
      request: (requestId: number) =>
        client.get<DailyRequestDetailResponse>(`/daily/requests/${requestId}`),
      updateRequest: (requestId: number, payload: DailyRequestPatchRequest) =>
        client.patch<DailyRequestDetailResponse>(`/daily/requests/${requestId}`, payload),
      reply: (requestId: number, text: string, idempotencyKey: string) =>
        client.post<DailyReplyResultResponse>(`/daily/requests/${requestId}/replies`, {
          text,
          idempotency_key: idempotencyKey,
        }),
      sendRequestItem: (requestId: number, itemId: number, idempotencyKey: string) =>
        client.post<DailySendToOwnerResponse>(
          `/daily/requests/${requestId}/items/${itemId}/send-to-me`,
          { idempotency_key: idempotencyKey },
        ),
    },
  };
}

export type Api = ReturnType<typeof createApi>;
