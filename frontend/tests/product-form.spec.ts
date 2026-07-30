/**
 * The edit dialog must emit a *patch*: only changed fields, with `null` meaning
 * "clear this", which is exactly what the API distinguishes.
 */

import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ProductForm from "@/components/ProductForm.vue";

import { makeProduct } from "./helpers";

function mountForm(product = makeProduct()) {
  return mount(ProductForm, { props: { product, saving: false } });
}

describe("ProductForm", () => {
  it("emits nothing but close when nothing changed", async () => {
    const wrapper = mountForm();

    await wrapper.find("form").trigger("submit");

    expect(wrapper.emitted("patch")).toBeUndefined();
    expect(wrapper.emitted("close")).toHaveLength(1);
  });

  it("emits only the fields that changed", async () => {
    const wrapper = mountForm();

    await wrapper.find("#field-title").setValue("Renamed");
    await wrapper.find("form").trigger("submit");

    expect(wrapper.emitted("patch")?.[0]).toEqual([{ title: "Renamed" }]);
  });

  it("clearing a price sends null, not an empty string", async () => {
    const wrapper = mountForm();

    await wrapper.find("#field-usdt").setValue("");
    await wrapper.find("form").trigger("submit");

    expect(wrapper.emitted("patch")?.[0]).toEqual([{ price_usdt: null }]);
  });

  it("refuses to submit a product without any price", async () => {
    const wrapper = mountForm();

    await wrapper.find("#field-stars").setValue("");
    await wrapper.find("#field-usdt").setValue("");
    await wrapper.find("form").trigger("submit");

    expect(wrapper.emitted("patch")).toBeUndefined();
    expect(wrapper.find(".error-text").text()).toContain("хотя бы одну цену");
  });

  it("refuses a slug a deep link cannot carry", async () => {
    const wrapper = mountForm();

    await wrapper.find("#field-slug").setValue("не слаг");
    await wrapper.find("form").trigger("submit");

    expect(wrapper.emitted("patch")).toBeUndefined();
    expect(wrapper.find(".error-text").text()).toContain("Slug");
  });

  it("refuses a relative delivery URL", async () => {
    const wrapper = mountForm();

    await wrapper.find("#field-url").setValue("t.me/+invite");
    await wrapper.find("form").trigger("submit");

    expect(wrapper.emitted("patch")).toBeUndefined();
    expect(wrapper.find(".error-text").text()).toContain("http");
  });

  it("creating a product emits the full payload", async () => {
    const wrapper = mount(ProductForm, { props: { product: null, saving: false } });

    await wrapper.find("#field-slug").setValue("pack18");
    await wrapper.find("#field-title").setValue("Sticker pack");
    await wrapper.find("#field-url").setValue("https://example.com/pack");
    await wrapper.find("#field-stars").setValue("120");
    await wrapper.find("form").trigger("submit");

    expect(wrapper.emitted("create")?.[0]).toEqual([
      {
        slug: "pack18",
        title: "Sticker pack",
        description: "",
        delivery_url: "https://example.com/pack",
        photo_file_id: null,
        price_stars: 120,
        price_usdt: null,
        is_active: true,
      },
    ]);
  });
});
