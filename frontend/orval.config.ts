import { defineConfig } from "orval";

// Regenerate with `bash scripts/web-api.sh` (dumps openapi.json from the backend first).
// Output is split per OpenAPI tag (one file per backend module) with shared model types in model/.
export default defineConfig({
  api: {
    input: "./openapi.json",
    output: {
      mode: "tags-split",
      target: "./src/api/generated/endpoints",
      schemas: "./src/api/generated/model",
      client: "react-query",
      clean: true,
      prettier: false,
      override: {
        mutator: { path: "./src/api/mutator.ts", name: "apiMutator" },
        query: { useQuery: true, useMutation: true },
      },
    },
  },
});
