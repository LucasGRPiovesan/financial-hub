import { Gauge, Plus, Receipt } from "lucide-react";
import { useState } from "react";

import type { MetricOut } from "../api/generated/model";
import { PageHeader } from "../components/page-header";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { EmptyState } from "../components/ui/empty-state";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { ArchiveMetricDialog, CreateMetricDialog } from "../features/billing/metric-dialogs";
import { flattenMetrics, useMetrics } from "../features/billing/metrics-api";
import { UsageSummaryPanel } from "../features/billing/usage-summary";
import { GatedButton } from "../features/console/gated-button";
import { NoProjectState } from "../features/console/no-project";
import { deniedReason } from "../features/console/permissions";
import { ListSkeleton, QueryErrorState } from "../features/console/states";
import { useProject } from "../features/console/use-project";
import { useDocumentTitle } from "../features/shell/document-title";
import { useI18n } from "../i18n";

export function BillingPage() {
  const { t } = useI18n();
  const { membership, can } = useProject();
  useDocumentTitle(t("billing.title"));
  if (!membership) {
    return (
      <>
        <PageHeader title={t("billing.title")} description={t("billing.description")} />
        <NoProjectState />
      </>
    );
  }
  if (!can("metering:read")) {
    return (
      <>
        <PageHeader title={t("billing.title")} description={t("billing.description")} />
        <EmptyState icon={Gauge} title={t("billing.metrics.noAccess")} description={deniedReason("metering:read", membership.role)} />
      </>
    );
  }
  return <BillingForProject projectId={membership.project_id} role={membership.role} mayManage={can("metering:manage")} />;
}

function BillingForProject({ projectId, role, mayManage }: { projectId: string; role: string; mayManage: boolean }) {
  const { t } = useI18n();
  const [creating, setCreating] = useState(false);
  const [archiving, setArchiving] = useState<MetricOut | null>(null);
  const metrics = useMetrics(projectId);
  const rows = flattenMetrics(metrics.data);
  const reason = deniedReason("metering:manage", role);

  let body;
  if (metrics.isPending) {
    body = <ListSkeleton label={t("billing.metrics.loading")} />;
  } else if (metrics.isError) {
    body = <QueryErrorState title={t("billing.metrics.loadFailed")} error={metrics.error} onRetry={() => void metrics.refetch()} />;
  } else if (rows.length === 0) {
    body = (
      <EmptyState
        icon={Gauge}
        title={t("billing.metrics.emptyTitle")}
        description={t("billing.metrics.emptyDescription")}
        action={
          mayManage ? (
            <Button onClick={() => setCreating(true)}>
              <Plus aria-hidden="true" />
              {t("billing.metrics.create")}
            </Button>
          ) : undefined
        }
      />
    );
  } else {
    body = (
      <>
        <div className="overflow-x-auto rounded-md border border-line bg-surface">
          <Table>
            <caption className="sr-only">{t("billing.metrics.caption")}</caption>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>{t("billing.metrics.cols.code")}</TableHead>
                <TableHead>{t("billing.metrics.cols.name")}</TableHead>
                <TableHead>{t("billing.metrics.cols.aggregation")}</TableHead>
                <TableHead>{t("billing.metrics.cols.field")}</TableHead>
                <TableHead>{t("billing.metrics.cols.status")}</TableHead>
                <TableHead>
                  <span className="sr-only">{t("billing.metrics.cols.actions")}</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((metric) => (
                <MetricRow key={metric.code} metric={metric} mayManage={mayManage} reason={reason} onArchive={() => setArchiving(metric)} />
              ))}
            </TableBody>
          </Table>
        </div>
        {metrics.hasNextPage ? (
          <div className="mt-3">
            <Button variant="secondary" onClick={() => void metrics.fetchNextPage()} disabled={metrics.isFetchingNextPage}>
              {t("billing.metrics.loadMore")}
            </Button>
          </div>
        ) : null}
      </>
    );
  }

  return (
    <>
      <PageHeader title={t("billing.title")} description={t("billing.description")} />

      <section aria-labelledby="usage-heading" className="flex flex-col gap-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h2 id="usage-heading" className="text-lg font-semibold tracking-tight">
              {t("billing.usage.title")}
            </h2>
            <p className="max-w-prose text-sm text-ink-muted">{t("billing.usage.description")}</p>
          </div>
          <GatedButton allowed={mayManage} reason={reason} onClick={() => setCreating(true)}>
            <Plus aria-hidden="true" />
            {t("billing.metrics.create")}
          </GatedButton>
        </div>

        <h3 className="sr-only">{t("billing.metrics.title")}</h3>
        {body}

        <UsageSummaryPanel projectId={projectId} />
      </section>

      <Card className="mt-8 border-dashed bg-surface-sunken shadow-none">
        <CardHeader className="border-b-0">
          <div className="flex flex-wrap items-center gap-2">
            <Receipt className="size-4 text-ink-faint" aria-hidden="true" />
            <CardTitle className="text-base">{t("billing.soon.title")}</CardTitle>
            <Badge tone="info">{t("billing.soon.badge")}</Badge>
          </div>
          <CardDescription>{t("billing.soon.description")}</CardDescription>
        </CardHeader>
      </Card>

      <CreateMetricDialog open={creating} projectId={projectId} onClose={() => setCreating(false)} />
      <ArchiveMetricDialog metric={archiving} projectId={projectId} onClose={() => setArchiving(null)} />
    </>
  );
}

function MetricRow({ metric, mayManage, reason, onArchive }: { metric: MetricOut; mayManage: boolean; reason: string; onArchive: () => void }) {
  const { t, tLoose } = useI18n();
  const archived = metric.status === "archived";
  return (
    <TableRow>
      <TableCell className="font-mono text-xs">{metric.code}</TableCell>
      <TableCell>
        <div className="flex flex-col gap-0.5">
          <span>{metric.name}</span>
          {metric.description ? <span className="text-xs text-ink-muted">{metric.description}</span> : null}
        </div>
      </TableCell>
      <TableCell>{tLoose("billing.metrics.aggregation", metric.aggregation)}</TableCell>
      <TableCell className="font-mono text-xs">{metric.field ?? <span className="text-ink-faint">{t("billing.metrics.noField")}</span>}</TableCell>
      <TableCell>
        <Badge tone={archived ? "neutral" : "success"}>{tLoose("billing.metrics.status", metric.status)}</Badge>
      </TableCell>
      <TableCell className="text-right">
        {archived ? null : (
          <GatedButton
            allowed={mayManage}
            reason={reason}
            variant="ghost"
            size="sm"
            onClick={onArchive}
            aria-label={t("billing.metrics.archiveNamed", { code: metric.code })}
          >
            {t("billing.metrics.archive")}
          </GatedButton>
        )}
      </TableCell>
    </TableRow>
  );
}
