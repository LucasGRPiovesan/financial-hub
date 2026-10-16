import { useQuery } from "@tanstack/react-query";

import { useT } from "../../i18n";

interface LiveResponse {
  status: string;
}

async function fetchLive(): Promise<LiveResponse> {
  const response = await fetch("/health/live");
  if (!response.ok) throw new Error(`API responded ${response.status}`);
  return (await response.json()) as LiveResponse;
}

export function HealthIndicator() {
  const t = useT();
  const { isPending, isError } = useQuery({
    queryKey: ["health", "live"],
    queryFn: fetchLive,
    refetchInterval: 10_000,
  });

  const label = isPending ? t("health.checking") : isError ? t("health.unreachable") : t("health.live");
  const color = isPending ? "bg-warning" : isError ? "bg-danger" : "bg-success";

  return (
    <p className="flex items-center gap-2 text-sm text-ink-muted" role="status">
      <span className={`inline-block size-2.5 rounded-full ${color}`} aria-hidden="true" />
      {label}
    </p>
  );
}
