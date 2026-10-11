export type Scope = "implemented" | "sandbox" | "simulated" | "planned";

export const SCOPES: readonly Scope[] = ["implemented", "sandbox", "simulated", "planned"];

export const SCOPE_LABELS: Record<Scope, string> = {
  implemented: "Implemented",
  sandbox: "Sandbox",
  simulated: "Simulated",
  planned: "Planned",
};

export const SCOPE_DESCRIPTIONS: Record<Scope, string> = {
  implemented: "Built and running against real data in this environment.",
  sandbox: "Runs against a provider's test environment; no real money moves.",
  simulated: "Behaviour is reproduced locally by Financial Hub; no external provider is called.",
  planned: "On the roadmap; not available yet.",
};
