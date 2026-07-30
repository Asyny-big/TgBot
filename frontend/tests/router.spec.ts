/**
 * The auth guard: a cold load tries the refresh cookie first, and only an
 * unauthenticated visitor is sent to the login screen.
 */

import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

const authState = {
  isAuthenticated: false,
  restore: vi.fn(),
};

vi.mock("@/stores/auth", () => ({ useAuthStore: () => authState }));
vi.mock("@/views/DashboardView.vue", () => ({ default: { template: "<div>dash</div>" } }));
vi.mock("@/views/LoginView.vue", () => ({ default: { template: "<div>login</div>" } }));
vi.mock("@/views/ProductsView.vue", () => ({ default: { template: "<div>products</div>" } }));
vi.mock("@/views/PurchasesView.vue", () => ({ default: { template: "<div>purchases</div>" } }));

const { router: appRouter } = await import("@/router");

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  authState.isAuthenticated = false;
  authState.restore.mockResolvedValue(false);
});

describe("router guard", () => {
  it("sends an unauthenticated visitor to the login screen and remembers the target", async () => {
    await appRouter.push("/products").catch(() => undefined);

    expect(authState.restore).toHaveBeenCalled();
    expect(appRouter.currentRoute.value.path).toBe("/login");
    expect(appRouter.currentRoute.value.query.redirect).toBe("/products");
  });

  it("lets an authenticated administrator through without asking again", async () => {
    authState.isAuthenticated = true;

    await appRouter.push("/purchases");

    expect(authState.restore).not.toHaveBeenCalled();
    expect(appRouter.currentRoute.value.path).toBe("/purchases");
  });

  it("keeps a signed-in administrator away from the login screen", async () => {
    authState.isAuthenticated = true;

    await appRouter.push("/login");

    expect(appRouter.currentRoute.value.path).toBe("/");
  });

  it("resumes a session from the refresh cookie on a cold start", async () => {
    authState.restore.mockImplementation(async () => {
      authState.isAuthenticated = true;
      return true;
    });

    await appRouter.push("/products");

    expect(authState.restore).toHaveBeenCalledOnce();
    expect(appRouter.currentRoute.value.path).toBe("/products");
  });

  it("redirects an unknown path to the dashboard", async () => {
    authState.isAuthenticated = true;

    await appRouter.push("/nope/deep");

    expect(appRouter.currentRoute.value.path).toBe("/");
  });
});
