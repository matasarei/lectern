import js from "@eslint/js";
import i18next from "eslint-plugin-i18next";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: { globals: globals.browser },
    plugins: { "react-hooks": reactHooks },
    rules: reactHooks.configs.recommended.rules,
  },
  {
    // Text shown to people goes through i18next, never straight into JSX.
    files: ["src/**/*.tsx"],
    ignores: ["src/**/*.test.tsx"],
    plugins: { i18next },
    rules: { "i18next/no-literal-string": ["error", { mode: "jsx-text-only" }] },
  },
);
