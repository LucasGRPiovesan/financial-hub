import { useQuery } from "@tanstack/react-query";

interface LiveResponse {
  status: string;
}

async function fetchLive(): Promise<LiveResponse> {
  const response = await fetch("/api/health/live");
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
  const color = isPending ? "bg-amber-400" : isError ? "bg-red-500" : "bg-emerald-500";

  return (
    <p className="flex items-center gap-2 text-sm text-slate-600" role="status">
      <span className={`inline-block size-2.5 rounded-full ${color}`} aria-hidden="true" />
      API: {label}
    </p>
  );
}
