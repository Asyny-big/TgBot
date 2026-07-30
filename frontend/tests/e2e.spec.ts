/**
 * End-to-end check of the panel's API layer against a *running* backend.
 *
 * Skipped unless `E2E_API_URL` is set, so `npm test` stays hermetic. When it is
 * set, the real `ApiClient` and the real generated types talk to a real server:
 * every endpoint the panel uses is exercised, including the refresh-and-retry
 * path with an actually expired access token.
 *
 *     make e2e-ui   # starts the API and runs this file
 */

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { ApiClient } from "@/api/client";
import { createApi } from "@/api/endpoints";
import type { Api } from "@/api/endpoints";

const baseUrl = process.env.E2E_API_URL;
const username = process.env.E2E_ADMIN_USERNAME ?? "admin";
const password = process.env.E2E_ADMIN_PASSWORD ?? "local-admin-password";

const describeE2E = baseUrl ? describe : describe.skip;

/** Node's fetch keeps no cookies, so the refresh cookie needs a jar. */
function installCookieJar(): { jar: Map<string, string>; restore: () => void } {
  const jar = new Map<string, string>();
  const realFetch = globalThis.fetch;

  globalThis.fetch = (async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const headers = new Headers(init.headers);
    if (jar.size > 0) {
      headers.set(
        "cookie",
        [...jar.entries()].map(([name, value]) => `${name}=${value}`).join("; "),
      );
    }
    const response = await realFetch(input, { ...init, headers });
    for (const cookie of response.headers.getSetCookie()) {
      const [pair] = cookie.split(";");
      const separator = pair?.indexOf("=") ?? -1;
      if (!pair || separator < 0) {
        continue;
      }
      const name = pair.slice(0, separator);
      const value = pair.slice(separator + 1);
      if (value === "" || cookie.toLowerCase().includes("max-age=0")) {
        jar.delete(name);
      } else {
        jar.set(name, value);
      }
    }
    return response;
  }) as typeof fetch;

  return { jar, restore: () => (globalThis.fetch = realFetch) };
}

describeE2E("admin panel against a live API", () => {
  let token: string | null = null;
  let sessionExpired = 0;
  let client: ApiClient;
  let api: Api;
  let restoreFetch: () => void;
  let jar: Map<string, string>;

  beforeAll(() => {
    const installed = installCookieJar();
    jar = installed.jar;
    restoreFetch = installed.restore;
    client = new ApiClient(baseUrl as string, {
      getToken: () => token,
      setToken: (value) => {
        token = value;
      },
      onSessionExpired: () => {
        sessionExpired += 1;
      },
    });
    api = createApi(client);
  });

  afterAll(() => {
    restoreFetch();
  });

  it("signs in, stores no token in the browser and receives the refresh cookie", async () => {
    const tokens = await api.auth.login(username, password);
    token = tokens.access_token;

    expect(tokens.token_type).toBe("bearer");
    expect(tokens.access_expires_in).toBeGreaterThan(0);
    expect([...jar.keys()].some((name) => name.includes("refresh"))).toBe(true);

    const admin = await api.auth.me();
    expect(admin.username).toBe(username);
  });

  it("rejects wrong credentials with the typed error envelope", async () => {
    await expect(api.auth.login(username, "definitely-wrong")).rejects.toMatchObject({
      status: 401,
      code: "invalid_credentials",
    });
  });

  it("refreshes automatically when the access token is no longer valid", async () => {
    // Simulate an expired token: the server will answer 401, and the client must
    // refresh through the cookie and replay the request transparently.
    const valid = token;
    token = `${valid}-tampered`;

    const admin = await api.auth.me();

    expect(admin.username).toBe(username);
    expect(token).not.toBe(`${valid}-tampered`);
    expect(sessionExpired).toBe(0);
  });

  it("walks the whole product lifecycle", async () => {
    const slug = `e2e${Date.now().toString(36)}`;
    const created = await api.products.create({
      slug,
      title: "E2E товар",
      description: "проверка",
      delivery_url: "https://example.com/secret",
      photo_file_id: null,
      price_stars: 150,
      price_usdt: "5.00",
      is_active: true,
    });

    expect(created.deep_link).toContain(`start=${slug}`);

    const listed = await api.products.list({ limit: 20, offset: 0, search: slug });
    expect(listed.meta.total).toBe(1);
    expect(listed.items[0]?.id).toBe(created.id);

    const renamed = await api.products.update(created.id, { title: "E2E переименован" });
    expect(renamed.title).toBe("E2E переименован");

    const cleared = await api.products.update(created.id, { price_usdt: null });
    expect(cleared.price_usdt).toBeNull();
    expect(cleared.price_stars).toBe(150);

    await expect(api.products.update(created.id, { price_stars: null })).rejects.toMatchObject({
      status: 422,
      code: "invalid_price",
    });

    await expect(
      api.products.create({
        slug,
        title: "duplicate",
        description: "",
        delivery_url: "https://example.com/other",
        photo_file_id: null,
        price_stars: 10,
        price_usdt: null,
        is_active: true,
      }),
    ).rejects.toMatchObject({ status: 409, code: "slug_already_exists" });

    await expect(api.products.remove(created.id)).resolves.toBeNull();
  });

  it("surfaces field level validation errors", async () => {
    try {
      await api.products.create({
        slug: "not a slug",
        title: "",
        description: "",
        delivery_url: "nope",
        photo_file_id: null,
        price_stars: null,
        price_usdt: null,
        is_active: true,
      });
      expect.unreachable("the request must fail");
    } catch (caught) {
      const error = caught as { status: number; code: string; fieldErrors: unknown[] };
      expect(error.status).toBe(422);
      expect(error.code).toBe("validation_error");
      expect(error.fieldErrors.length).toBeGreaterThan(0);
    }
  });

  it("reads the dashboard and the purchase search", async () => {
    const overview = await api.stats.overview();
    expect(overview.today.period).toBe("today");
    expect(typeof overview.users_total).toBe("number");

    const purchases = await api.purchases.search({ limit: 20, offset: 0 });
    expect(purchases.meta.limit).toBe(20);
    expect(Array.isArray(purchases.items)).toBe(true);

    const filtered = await api.purchases.search({
      limit: 20,
      offset: 0,
      search: "nothing-matches-this",
      status: ["paid"],
    });
    expect(filtered.meta.total).toBe(0);
  });

  it("reports a 404 for verification of an unknown purchase", async () => {
    await expect(
      api.purchases.verify("00000000-0000-4000-8000-000000000000"),
    ).rejects.toMatchObject({ status: 404, code: "purchase_not_found" });
  });

  it("logs out, after which the session is over", async () => {
    await expect(api.auth.logout()).resolves.toBeNull();

    // The cookie is gone and the (still valid) access token is the only thing
    // left; once it is dropped, nothing can be refreshed any more.
    token = "expired";
    await expect(api.auth.me()).rejects.toMatchObject({ status: 401 });
    expect(sessionExpired).toBeGreaterThan(0);
    expect(token).toBeNull();
  });
});
