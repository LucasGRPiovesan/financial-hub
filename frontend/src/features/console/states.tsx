import { TriangleAlert } from "lucide-react";

import { Button } from "../../components/ui/button";
import { EmptyState } from "../../components/ui/empty-state";
import { Skeleton } from "../../components/ui/skeleton";
import { errorText } from "./errors";

/** Placeholder rows while a list loads. Announced once, not per row. */
export function ListSkeleton({ rows = 4, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div role="status" aria-label={label} className="flex flex-col gap-2 rounded-md border border-line bg-surface p-4">
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className="h-8 w-full" />
      ))}
    </div>
  );
}

/** A failed read: the problem's message and a retry. */
export function QueryErrorState({ error, onRetry, title }: { error: unknown; onRetry: () => void; title: string }) {
  return (
    <EmptyState
      icon={TriangleAlert}
      title={title}
      description={errorText(error)}
      action={
        <Button variant="secondary" onClick={onRetry}>
          Try again
        </Button>
      }
    />
  );
}
