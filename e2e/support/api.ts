import { request, type APIRequestContext } from "@playwright/test";

import type { Persona } from "./personas";

interface DemoOperator {
  id: string;
  email: string;
}

/** Sign in as a demo persona through the API; returns the context that now holds the session cookie. */
export async function demoLogin(baseURL: string, persona: Persona): Promise<APIRequestContext> {
  const api = await request.newContext({ baseURL });
  const fail = async (message: string): Promise<never> => {
    await api.dispose();
    throw new Error(message);
  };

  const listed = await api.get("/v1/auth/demo-operators");
  if (!listed.ok()) {
    return fail(
      `GET /v1/auth/demo-operators -> ${listed.status()}. Is FH_DEMO_LOGIN=true and the demo workspace seeded (scripts/e2e.sh does both)?`,
    );
  }
  const { operators } = (await listed.json()) as { operators: DemoOperator[] };
  const operator = operators.find((candidate) => candidate.email === persona.email);
  if (!operator) {
    return fail(`Demo operator ${persona.email} not found; run: docker compose run --rm api python -m financial_hub.cli seed-demo`);
  }
  const login = await api.post("/v1/auth/demo-login", { data: { operator_id: operator.id } });
  if (!login.ok()) return fail(`POST /v1/auth/demo-login -> ${login.status()}`);
  return api;
}
