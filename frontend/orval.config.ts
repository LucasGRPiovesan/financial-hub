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
        // Defaults per verb: GET -> useQuery, everything else -> useMutation. The mutator returns the
        // parsed body, so the generated types must not wrap it in a {data, status} envelope.
        fetch: { includeHttpResponseReturnType: false },
      },
    },
  },
});
