import js from "@eslint/js";
import globals from "globals";
import pluginVue from "eslint-plugin-vue";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/**", "node_modules/**", "src/api/schema.d.ts"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs["flat/recommended"],
  {
    files: ["**/*.vue"],
    languageOptions: { parserOptions: { parser: tseslint.parser } },
  },
  {
    // Application code runs in the browser; the build config runs in Node.
    files: ["src/**/*.{ts,vue}", "tests/**/*.ts"],
    languageOptions: { globals: { ...globals.browser } },
  },
  {
    files: ["*.config.ts", "*.config.js"],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    rules: {
      // TypeScript already resolves every identifier; the core rule only
      // duplicates that check and misbehaves with ambient DOM types.
      "no-undef": "off",
      "@typescript-eslint/consistent-type-imports": "error",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      "vue/multi-word-component-names": "off",
      eqeqeq: ["error", "always"],
    },
  },
);
