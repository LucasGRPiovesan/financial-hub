import { HealthIndicator } from "../features/health/health-indicator";

export function HomePage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col justify-center gap-4 p-8">
      <h1 className="text-3xl font-semibold tracking-tight">Financial Hub</h1>
      <HealthIndicator />
    </main>
  );
}
