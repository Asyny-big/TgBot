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
export type PageMeta = Schemas["PageMeta"];
export type PurchaseStatus = Schemas["PurchaseStatus"];
export type PaymentProvider = Schemas["PaymentProvider"];
export type VerificationOutcome = Schemas["VerificationOutcome"];

export type ProductPage = Schemas["PageResponse_ProductResponse_"];
export type PurchasePage = Schemas["PageResponse_PurchaseRecordResponse_"];

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

    stats: {
      overview: () => client.get<OverviewResponse>("/stats/overview"),
    },
  };
}

export type Api = ReturnType<typeof createApi>;
