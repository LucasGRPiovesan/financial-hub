import type { ErrorComponentProps } from "@tanstack/react-router";
import { TriangleAlert } from "lucide-react";

import { EmptyState } from "../components/ui/empty-state";
import { Skeleton } from "../components/ui/skeleton";
import { BrandMark } from "../features/shell/sidebar-nav";
import { useT } from "../i18n";
import { isApiError } from "../lib/api";

/** Shell silhouette shown while the session resolves on a hard load, so the screen is never blank. */
export function BootScreen() {
  const t = useT();
  return (
    <div role="status" aria-label={t("boot.loading")} className="min-h-dvh lg:grid lg:grid-cols-[15rem_minmax(0,1fr)]">
      <aside className="hidden h-dvh flex-col bg-chrome px-5 py-5 lg:flex">
        <BrandMark />
        <div aria-hidden="true" className="mt-8 flex flex-col gap-3">
          {[0, 1, 2, 3, 4, 5].map((row) => (
            <div key={row} className="skeleton-shimmer h-4 rounded-sm bg-chrome-raised" style={{ width: `${55 + ((row * 17) % 35)}%` }} />
          ))}
        </div>
      </aside>
      <div className="flex min-w-0 flex-col" aria-hidden="true">
        <div className="flex h-14 items-center gap-3 border-b border-line bg-surface px-5">
          <Skeleton className="h-6 w-40" />
          <Skeleton className="ml-auto size-8 rounded-full" />
        </div>
        <div className="w-full px-4 py-6 sm:px-6 sm:py-8 xl:px-8 2xl:px-10">
          <Skeleton className="h-7 w-48" />
          <div className="ledger-rule mt-4 mb-6" />
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {[0, 1, 2, 3].map((card) => (
              <Skeleton key={card} className="h-28 rounded-md" />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

export function RouteError({ error }: ErrorComponentProps) {
  const t = useT();
  const detail =
    isApiError(error) && error.isNetworkError
      ? t("routeError.network")
      : error instanceof Error
        ? error.message
        : t("routeError.unexpected");
  return (
    <div className="mx-auto max-w-xl p-8">
      <EmptyState icon={TriangleAlert} title={t("routeError.title")} description={detail} />
    </div>
  );
}
