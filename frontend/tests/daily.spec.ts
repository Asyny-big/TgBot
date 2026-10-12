/**
 * Daily Content in the panel: the stores keep their promises — a retried
 * answer or "send it to me" reuses its idempotency key, a stale version
 * reloads the request, a slow page never overwrites a newer one — and the
 * pages show what the API returned, never a file.
 */

import { mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";

import {
  makeDailyContent,
  makeDailyOverview,
  makeDailyRequest,
  makeDailyRequestDetail,
  makeDailySubscriberDetail,
  page,
  stubDailyApi,
} from "./dailyHelpers";
import { usePiniaForEachTest } from "./helpers";

const daily = stubDailyApi();

vi.mock("@/api", async () => {
  const client = await import("@/api/client");
  return { api: { daily }, ApiError: client.ApiError };
});

const {
  useDailyLibraryStore,
  useDailyOverviewStore,
  useDailyRequestStore,
  useDailySubscriberStore,
} = await import("@/stores/daily");
const { useToastStore } = await import("@/stores/toasts");
const DailyRequestsView = (await import("@/views/DailyRequestsView.vue")).default;
const DailyLibraryView = (await import("@/views/DailyLibraryView.vue")).default;
const DailyDashboardView = (await import("@/views/DailyDashboardView.vue")).default;

usePiniaForEachTest();

beforeEach(() => {
  vi.clearAllMocks();
});

function conflict(code: string): ApiError {
  return new ApiError(409, code, "conflict", { current_version: 2 });
}

function deferred<ValueT>() {
  let resolve!: (value: ValueT) => void;
  const promise = new Promise<ValueT>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

// --------------------------------------------------------------------------- //
// Stores                                                                      //
// --------------------------------------------------------------------------- //

describe("daily request store", () => {
  it("saves against the version on screen", async () => {
    const store = useDailyRequestStore();
    daily.request.mockResolvedValue(makeDailyRequestDetail());
    daily.updateRequest.mockResolvedValue(makeDailyRequestDetail({ version: 2, status: "in_review" }));
    await store.open(7);

    const saved = await store.save({ status: "in_review", admin_note: "checking" });

    expect(saved).toBe(true);
    expect(daily.updateRequest).toHaveBeenCalledWith(7, {
      version: 1,
      status: "in_review",
      admin_note: "checking",
    });
    expect(store.detail?.request.version).toBe(2);
  });

  it("reloads the request when another tab saved first", async () => {
    const store = useDailyRequestStore();
    daily.request
      .mockResolvedValueOnce(makeDailyRequestDetail())
      .mockResolvedValueOnce(makeDailyRequestDetail({ version: 2, status: "completed" }));
    daily.updateRequest.mockRejectedValue(conflict("daily_request_version_conflict"));
    await store.open(7);

    const saved = await store.save({ status: "rejected" });

    expect(saved).toBe(false);
    expect(daily.request).toHaveBeenCalledTimes(2);
    expect(store.detail?.request.status).toBe("completed");
    expect(store.detail?.request.version).toBe(2);
    expect(useToastStore().items.at(-1)?.text).toContain("в другой вкладке");
  });

  it("reuses the answer's key when the same answer is retried", async () => {
    const store = useDailyRequestStore();
    daily.request.mockResolvedValue(makeDailyRequestDetail());
    daily.reply
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce({ created: true, reply: {} });
    await store.open(7);

    expect(await store.reply("Thanks!")).toBe(false);
    expect(await store.reply("  Thanks!  ")).toBe(true);
    await store.reply("Another answer");

    const keys = daily.reply.mock.calls.map((call) => call[2]);
    expect(keys[0]).toBe(keys[1]); // the retry: the same answer
    expect(keys[2]).not.toBe(keys[1]); // confirmed: the next one is new
    expect(daily.reply.mock.calls[1]?.[1]).toBe("Thanks!");
  });

  it("gives an edited answer a key of its own", async () => {
    const store = useDailyRequestStore();
    daily.request.mockResolvedValue(makeDailyRequestDetail());
    daily.reply.mockRejectedValue(new TypeError("Failed to fetch"));
    await store.open(7);

    await store.reply("Thanks!");
    await store.reply("Thanks a lot!");

    const keys = daily.reply.mock.calls.map((call) => call[2]);
    expect(keys[0]).not.toBe(keys[1]);
  });

  it("keeps the attachment's key until the send is confirmed", async () => {
    const store = useDailyRequestStore();
    daily.request.mockResolvedValue(makeDailyRequestDetail());
    daily.sendRequestItem
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValue({ created: true, messages: [] });
    await store.open(7);

    await store.sendItem(32);
    await store.sendItem(32);
    await store.sendItem(32);

    const keys = daily.sendRequestItem.mock.calls.map((call) => call[2]);
    expect(keys[0]).toBe(keys[1]);
    expect(keys[2]).not.toBe(keys[1]);
  });

  it("never lets a slow page overwrite a newer one", async () => {
    const store = useDailyRequestStore();
    const slow = deferred<ReturnType<typeof page>>();
    daily.requests
      .mockReturnValueOnce(slow.promise)
      .mockResolvedValueOnce(page([makeDailyRequest({ id: 2 })]));

    const first = store.applyFilters({ status: "new" });
    await store.applyFilters({ status: "completed" });
    slow.resolve(page([makeDailyRequest({ id: 1 })]));
    await first;

    expect(store.items.map((item) => item.id)).toEqual([2]);
    expect(daily.requests).toHaveBeenLastCalledWith({ limit: 20, offset: 0, status: "completed" });
  });
});

describe("daily library store", () => {
  it("changes an item in place", async () => {
    const store = useDailyLibraryStore();
    daily.library.mockResolvedValue(page([makeDailyContent()]));
    daily.changeStatus.mockResolvedValue(makeDailyContent({ status: "disabled" }));
    await store.fetchPage();

    await store.changeStatus(store.items[0]!, "disable");

    expect(daily.changeStatus).toHaveBeenCalledWith(12, "disable");
    expect(store.items[0]?.status).toBe("disabled");
  });

  it("reloads the list when the item moved meanwhile", async () => {
    const store = useDailyLibraryStore();
    daily.library.mockResolvedValue(page([makeDailyContent()]));
    daily.changeStatus.mockRejectedValue(conflict("daily_library_action_not_allowed"));
    await store.fetchPage();

    await store.changeStatus(store.items[0]!, "restore");

    expect(daily.library).toHaveBeenCalledTimes(2);
    expect(useToastStore().items.at(-1)?.text).toContain("недоступно");
  });

  it("sends an item to the owner once per click, even when retried", async () => {
    const store = useDailyLibraryStore();
    daily.sendLibraryItem
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValue({ created: true, messages: [] });
    const item = makeDailyContent();

    await store.sendToMe(item);
    await store.sendToMe(item);
    await store.sendToMe(item);

    const keys = daily.sendLibraryItem.mock.calls.map((call) => call[1]);
    expect(keys[0]).toBe(keys[1]);
    expect(keys[2]).not.toBe(keys[1]);
  });
});

describe("daily subscriber store", () => {
  it("bans and shows the ban at once", async () => {
    const store = useDailySubscriberStore();
    daily.setRequestsBan.mockResolvedValue(
      makeDailySubscriberDetail({
        requests_banned_at: "2026-10-11T09:00:00Z",
        requests_banned_by: "admin:root",
        can_suggest: false,
      }),
    );

    await store.setBan(4242, true);

    expect(daily.setRequestsBan).toHaveBeenCalledWith(4242, true);
    expect(store.detail?.subscriber.requests_banned_by).toBe("admin:root");
  });
});

describe("daily overview store", () => {
  it("loads the overview", async () => {
    const store = useDailyOverviewStore();
    daily.overview.mockResolvedValue(makeDailyOverview());

    await store.load();

    expect(store.overview?.subscriptions.active).toBe(40);
  });
});

// --------------------------------------------------------------------------- //
// Pages                                                                       //
// --------------------------------------------------------------------------- //

describe("DailyRequestsView", () => {
  it("shows the request's messages and offers to send only files to the owner", async () => {
    daily.requests.mockResolvedValue(page([makeDailyRequest()]));
    daily.request.mockResolvedValue(makeDailyRequestDetail());
    const wrapper = mount(DailyRequestsView);
    await useDailyRequestStore().open(7);
    await wrapper.vm.$nextTick();

    const text = wrapper.text();
    expect(text).toContain("More yoga, please");
    expect(text).toContain("@kate");
    const sendButtons = wrapper
      .findAll("button")
      .filter((button) => button.text().includes("Прислать мне"));
    expect(sendButtons).toHaveLength(1); // the photo; the text is right there
    expect(wrapper.html()).not.toContain("file_id");

    await sendButtons[0]!.trigger("click");
    expect(daily.sendRequestItem).toHaveBeenCalledWith(7, 32, expect.any(String));
  });

  it("answers with the text typed and keeps the button off while it is empty", async () => {
    daily.requests.mockResolvedValue(page([makeDailyRequest()]));
    daily.request.mockResolvedValue(makeDailyRequestDetail());
    daily.reply.mockResolvedValue({ created: true, reply: {} });
    const wrapper = mount(DailyRequestsView);
    await useDailyRequestStore().open(7);
    await wrapper.vm.$nextTick();
    const answer = () => wrapper.findAll("button").find((button) => button.text() === "Ответить");

    expect(answer()?.attributes("disabled")).toBeDefined();
    await wrapper.find("textarea[aria-label='Текст ответа']").setValue("Added, thanks!");
    await answer()?.trigger("click");

    expect(daily.reply).toHaveBeenCalledWith(7, "Added, thanks!", expect.any(String));
  });

  it("starts on the new requests", () => {
    daily.requests.mockResolvedValue(page([]));

    mount(DailyRequestsView);

    expect(daily.requests).toHaveBeenCalledWith({ limit: 20, offset: 0, status: "new" });
  });
});

describe("DailyLibraryView", () => {
  it("offers what each status allows, and the preview through the bot", async () => {
    daily.library.mockResolvedValue(
      page([makeDailyContent(), makeDailyContent({ id: 13, status: "archived" })]),
    );
    const wrapper = mount(DailyLibraryView);
    await vi.waitFor(() => expect(wrapper.findAll("tbody tr")).toHaveLength(2));

    const [active, archived] = wrapper.findAll("tbody tr");
    expect(active!.text()).toContain("Отключить");
    expect(active!.text()).toContain("В архив");
    expect(archived!.text()).toContain("Восстановить");
    expect(archived!.text()).not.toContain("Отключить");
    expect(active!.text()).toContain("📨 Прислать мне");
    expect(active!.text()).toContain("stretch.mp4");
  });
});

describe("DailyDashboardView", () => {
  it("shows the counters and the firing alert", async () => {
    daily.overview.mockResolvedValue(makeDailyOverview());
    const wrapper = mount(DailyDashboardView);
    await vi.waitFor(() => expect(wrapper.text()).toContain("40 активных"));

    const text = wrapper.text();
    expect(text).toContain("30 отправлено");
    expect(text).toContain("4 новых");
    expect(text).toContain("Мало материалов");
    expect(text).toContain("активных: 1");
  });
});
