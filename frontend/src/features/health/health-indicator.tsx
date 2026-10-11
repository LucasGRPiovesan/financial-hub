import { useQuery } from "@tanstack/react-query";

interface LiveResponse {
  status: string;
}

async function fetchLive(): Promise<LiveResponse> {
  const response = await fetch("/health/live");
  if (!response.ok) throw new Error(`API responded ${response.status}`);
  return (await response.json()) as LiveResponse;
}

export function HealthIndicator() {
  const { isPending, isError } = useQuery({
    queryKey: ["health", "live"],
    queryFn: fetchLive,
    refetchInterval: 10_000,
  });

  const label = isPending ? "checking" : isError ? "unreachable" : "live";
  const color = isPending ? "bg-warning" : isError ? "bg-danger" : "bg-success";

  return (
    <p className="flex items-center gap-2 text-sm text-ink-muted" role="status">
      <span className={`inline-block size-2.5 rounded-full ${color}`} aria-hidden="true" />
      API: {label}
    </p>
  );
}
