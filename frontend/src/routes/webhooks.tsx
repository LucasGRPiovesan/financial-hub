import { Link } from "@tanstack/react-router";
import { Lock, Plus, Webhook } from "lucide-react";
import { useState } from "react";

import { DateTime } from "../components/date-time";
import { PageHeader } from "../components/page-header";
import { EmptyState } from "../components/ui/empty-state";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { GatedButton } from "../features/console/gated-button";
import { NoProjectState } from "../features/console/no-project";
import { deniedReason } from "../features/console/permissions";
import { ListSkeleton, QueryErrorState } from "../features/console/states";
import { useProject } from "../features/console/use-project";
import { useDocumentTitle } from "../features/shell/document-title";
import { CreateEndpointDialog } from "../features/webhooks/endpoint-dialogs";
import { EndpointStatusBadge, EventFilterChips } from "../features/webhooks/webhook-parts";
import { useWebhookEndpoints } from "../features/webhooks/webhooks-api";
import { useI18n } from "../i18n";

// Routes registered outside the router's static route types use plain-string paths.
const ENDPOINT_PATH: string = "/webhooks/$endpointId";

export function WebhooksPage() {
  const { t } = useI18n();
  const { membership, can } = useProject();
  useDocumentTitle(t("webhooks.title"));
  if (!membership) return <NoProjectState />;
  return <WebhooksForProject projectId={membership.project_id} role={membership.role} mayRead={can("webhooks:read")} mayManage={can("webhooks:manage")} />;
}

function WebhooksForProject({ projectId, role, mayRead, mayManage }: { projectId: string; role: string; mayRead: boolean; mayManage: boolean }) {
  const { t } = useI18n();
  const endpoints = useWebhookEndpoints(projectId, mayRead);
  const [creating, setCreating] = useState(false);

  const header = (
    <PageHeader
      title={t("webhooks.title")}
      description={t("webhooks.description")}
      actions={
        <GatedButton allowed={mayManage} reason={deniedReason("webhooks:manage", role)} onClick={() => setCreating(true)}>
          <Plus aria-hidden="true" />
          {t("webhooks.add")}
        </GatedButton>
      }
    />
  );

  if (!mayRead) {
    return (
      <>
        {header}
        <EmptyState icon={Lock} title={t("webhooks.noAccessTitle")} description={deniedReason("webhooks:read", role)} />
      </>
    );
  }

  return (
    <>
      {header}
      {endpoints.isPending ? (
        <ListSkeleton label={t("webhooks.loading")} />
      ) : endpoints.isError ? (
        <QueryErrorState title={t("webhooks.loadFailed")} error={endpoints.error} onRetry={() => void endpoints.refetch()} />
      ) : endpoints.data.data.length === 0 ? (
        <EmptyState icon={Webhook} title={t("webhooks.emptyTitle")} description={t("webhooks.emptyDescription")} />
      ) : (
        <div className="overflow-x-auto rounded-md border border-line bg-surface">
          <Table>
            <caption className="sr-only">{t("webhooks.caption")}</caption>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>{t("webhooks.cols.url")}</TableHead>
                <TableHead>{t("webhooks.cols.description")}</TableHead>
                <TableHead>{t("webhooks.cols.events")}</TableHead>
                <TableHead>{t("webhooks.cols.status")}</TableHead>
                <TableHead>{t("webhooks.cols.created")}</TableHead>
                <TableHead>
                  <span className="sr-only">{t("webhooks.cols.actions")}</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {endpoints.data.data.map((endpoint) => (
                <TableRow key={endpoint.id}>
                  <TableCell className="max-w-72">
                    <Link
                      to={ENDPOINT_PATH}
                      params={{ endpointId: endpoint.id }}
                      aria-label={t("webhooks.openNamed", { url: endpoint.url })}
                      className="block truncate rounded-xs font-mono text-xs text-primary underline-offset-2 hover:underline"
                      title={endpoint.url}
                    >
                      {endpoint.url}
                    </Link>
                  </TableCell>
                  <TableCell className="max-w-56 truncate text-sm" title={endpoint.description ?? undefined}>
                    {endpoint.description ?? <span className="text-ink-faint">{t("webhooks.noDescription")}</span>}
                  </TableCell>
                  <TableCell>
                    <EventFilterChips eventTypes={endpoint.event_types} />
                  </TableCell>
                  <TableCell>
                    <EndpointStatusBadge enabled={endpoint.enabled} />
                  </TableCell>
                  <TableCell>
                    <DateTime value={endpoint.created_at} />
                  </TableCell>
                  <TableCell>
                    <div className="flex justify-end">
                      <Link
                        to={ENDPOINT_PATH}
                        params={{ endpointId: endpoint.id }}
                        className="rounded-xs text-sm text-primary underline-offset-2 hover:underline"
                        aria-hidden="true"
                        tabIndex={-1}
                      >
                        {t("webhooks.open")}
                      </Link>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {endpoints.data.has_more ? <p className="border-t border-line px-3 py-2 text-xs text-ink-muted">{t("webhooks.firstOnly")}</p> : null}
        </div>
      )}

      <CreateEndpointDialog projectId={projectId} open={creating} onOpenChange={setCreating} />
    </>
  );
}
