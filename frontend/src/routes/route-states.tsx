import type { ErrorComponentProps } from "@tanstack/react-router";
import { TriangleAlert } from "lucide-react";

import { EmptyState } from "../components/ui/empty-state";
import { Skeleton } from "../components/ui/skeleton";
import { isApiError } from "../lib/api";

export function PendingShell() {
  return (
    <div className="p-8" role="status" aria-label="Loading">
      <Skeleton className="h-8 w-48" />
    </div>
  );
}

export function RouteError({ error }: ErrorComponentProps) {
  const detail =
    isApiError(error) && error.isNetworkError
      ? "The API could not be reached."
      : error instanceof Error
        ? error.message
        : "Unexpected error.";
  return (
    <div className="mx-auto max-w-xl p-8">
      <EmptyState icon={TriangleAlert} title="This page couldn't load" description={detail} />
    </div>
  );
}
