import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "src/api/generated"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    plugins: { "react-hooks": reactHooks, "react-refresh": reactRefresh },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      "@typescript-eslint/no-explicit-any": "error",
    },
  },
  {
    // ADR-0002: money is integer minor units. Amounts must never pass through a floating-point value, so
    // the money module and component may not coerce with Number()/parseFloat()/parseInt()/unary plus.
    files: ["src/lib/money.ts", "src/components/money.tsx"],
    rules: {
      "no-restricted-globals": [
        "error",
        { name: "Number", message: "Money must not be converted to a float (ADR-0002); use BigInt." },
        { name: "parseFloat", message: "Money must not be converted to a float (ADR-0002); use BigInt." },
        { name: "parseInt", message: "Money must not be converted to a float (ADR-0002); use BigInt." },
      ],
      "no-restricted-properties": [
        "error",
        { object: "Number", property: "parseFloat", message: "Money must not be converted to a float (ADR-0002)." },
        { object: "Number", property: "parseInt", message: "Money must not be converted to a float (ADR-0002)." },
        { object: "Math", property: "round", message: "Money is never rounded in the UI (ADR-0002)." },
        { object: "Math", property: "floor", message: "Money is never rounded in the UI (ADR-0002)." },
        { object: "Math", property: "ceil", message: "Money is never rounded in the UI (ADR-0002)." },
        { object: "Math", property: "trunc", message: "Money is never rounded in the UI (ADR-0002)." },
      ],
      "no-restricted-syntax": [
        "error",
        {
          selector: "UnaryExpression[operator='+']",
          message: "Unary plus coerces to a float; use BigInt (ADR-0002).",
        },
        {
          selector: "CallExpression[callee.property.name=/^(toFixed|toPrecision)$/]",
          message: "toFixed/toPrecision operate on floats; use BigInt string splitting (ADR-0002).",
        },
      ],
    },
  },
);
