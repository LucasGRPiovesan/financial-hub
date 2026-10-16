/** The demo workspace operators (`python -m financial_hub.cli seed-demo`, docs/DEVELOPMENT.md "Demo mode"). */
export type Role = "owner" | "admin" | "finance" | "viewer";

export interface Persona {
  key: "ana" | "bruno" | "carla" | "diego";
  displayName: string;
  firstName: string;
  email: string;
  role: Role;
}

export const PERSONAS = {
  ana: { key: "ana", displayName: "Ana Owner", firstName: "Ana", email: "ana@demo.financial-hub.local", role: "owner" },
  bruno: { key: "bruno", displayName: "Bruno Admin", firstName: "Bruno", email: "bruno@demo.financial-hub.local", role: "admin" },
  carla: { key: "carla", displayName: "Carla Finance", firstName: "Carla", email: "carla@demo.financial-hub.local", role: "finance" },
  diego: { key: "diego", displayName: "Diego Viewer", firstName: "Diego", email: "diego@demo.financial-hub.local", role: "viewer" },
} as const satisfies Record<string, Persona>;

export const PERSONA_LIST: readonly Persona[] = Object.values(PERSONAS);

export const DEMO_PROJECT_NAME = "Demo Store";
