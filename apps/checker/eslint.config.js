import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

// Rulebook §4: strict TypeScript, no `any`, no `@ts-ignore`.
export default tseslint.config(
  { ignores: ["node_modules", ".wrangler"] },
  {
    files: ["**/*.ts"],
    extends: [js.configs.recommended, ...tseslint.configs.strictTypeChecked],
    languageOptions: {
      globals: globals.serviceworker,
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    rules: {
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/ban-ts-comment": ["error", { "ts-expect-error": true, "ts-ignore": true, "ts-nocheck": true }],
      "@typescript-eslint/restrict-template-expressions": ["error", { allowNumber: true }],
    },
  },
);
