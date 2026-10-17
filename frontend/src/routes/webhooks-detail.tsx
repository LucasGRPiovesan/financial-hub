import { Link, useParams } from "@tanstack/react-router";
import { ArrowLeft, Lock, RefreshCw, Send, Webhook } from "lucide-react";
import { useState, type ReactNode } from "react";

import type { WebhookEndpointOut } from "../api/generated/model";
import { ConfirmDialog } from "../components/confirm-dialog";
import { DateTime } from "../components/date-time";
import { PageHeader } from "../components/page-header";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { EmptyState } from "../components/ui/empty-state";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { useToast } from "../components/ui/use-toast";
import { errorText } from "../features/console/errors";
import { GatedButton } from "../features/console/gated-button";
import { NoProjectState } from "../features/console/no-project";
import { deniedReason } from "../features/console/permissions";
import { ListSkeleton, QueryErrorState } from "../features/console/states";
import { useProject } from "../features/console/use-project";
import { useDocumentTitle } from "../features/shell/document-title";
import { EditEndpointDialog, RotatedSecretDialog } from "../features/webhooks/endpoint-dialogs";
import { AttemptOutcomeBadge, EndpointStatusBadge, EventFilterChips } from "../features/webhooks/webhook-parts";
import {
  useDisableWebhookEndpoint,
  useEnableWebhookEndpoint,
  useRotateWebhookSecret,
  useSendWebhookTest,
  useWebhookAttempts,
  useWebhookEndpoint,
} from "../features/webhooks/webhooks-api";
import { useI18n } from "../i18n";
import { isApiError } from "../lib/api";

// Routes registered outside the router's static route types use plain-string paths.
const WEBHOOKS_PATH: string = "/webhooks";
const EVENT_PATH: string = "/events/$eventId";

export function WebhookDetailPage() {
  const { t } = useI18n();
  const { membership, can } = useProject();
  const { endpointId } = useParams({ strict: false });
  useDocumentTitle(t("webhooks.detail.title"));
  if (!membership) return <NoProjectState />;
  if (!can("webhooks:read")) {
    return <EmptyState icon={Lock} title={t("webhooks.noAccessTitle")} description={deniedReason("webhooks:read", membership.role)} />;
  }
  if (!endpointId) return null;
  return <WebhookDetail projectId={membership.project_id} endpointId={endpointId} role={membership.role} mayManage={can("webhooks:manage")} />;
}

function BackLink() {
  const { t } = useI18n();
  return (
    <Link to={WEBHOOKS_PATH} className="mb-3 inline-flex items-center gap-1 rounded-xs text-sm text-primary underline-offset-2 hover:underline">
      <ArrowLeft className="size-4" aria-hidden="true" />
      {t("webhooks.detail.back")}
    </Link>
  );
}

function WebhookDetail({ projectId, endpointId, role, mayManage }: { projectId: string; endpointId: string; role: string; mayManage: boolean }) {
  const { t } = useI18n();
  const query = useWebhookEndpoint(projectId, endpointId);
  if (query.isPending) {
    return (
      <>
        <BackLink />
        <ListSkeleton label={t("webhooks.detail.loading")} />
      </>
    );
  }
  if (query.isError) {
    if (isApiError(query.error) && query.error.status === 404) {
      return (
        <>
          <BackLink />
          <EmptyState icon={Webhook} title={t("webhooks.detail.notFoundTitle")} description={t("webhooks.detail.notFoundDescription")} />
        </>
      );
    }
    return (
      <>
        <BackLink />
        <QueryErrorState title={t("webhooks.detail.loadFailed")} error={query.error} onRetry={() => void query.refetch()} />
      </>
    );
  }
  return <EndpointView projectId={projectId} endpoint={query.data} role={role} mayManage={mayManage} />;
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium tracking-wide text-ink-muted uppercase">{label}</dt>
      <dd className="mt-0.5 text-sm">{children}</dd>
    </div>
  );
}

function EndpointView({ projectId, endpoint, role, mayManage }: { projectId: string; endpoint: WebhookEndpointOut; role: string; mayManage: boolean }) {
  const { t } = useI18n();
  const toast = useToast();
  const reason = deniedReason("webhooks:manage", role);
  const disable = useDisableWebhookEndpoint(projectId, endpoint.id);
  const enable = useEnableWebhookEndpoint(projectId, endpoint.id);
  const rotate = useRotateWebhookSecret(projectId, endpoint.id);
  const sendTest = useSendWebhookTest(projectId, endpoint.id);
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState<"disable" | "rotate" | null>(null);
  // The rotated secret lives in component state only, never in a query or mutation cache.
  const [rotatedSecret, setRotatedSecret] = useState<string | null>(null);
  const [testEventId, setTestEventId] = useState<string | null>(null);

  function confirmDisable() {
    disable.mutate(undefined, {
      onSuccess: () => {
        toast({ tone: "success", title: t("webhooks.disableDialog.done"), description: endpoint.url });
        setConfirming(null);
      },
      onError: () => toast({ tone: "danger", title: t("webhooks.disableDialog.failed") }),
    });
  }

  function confirmRotate() {
    rotate.mutate(
      { onSecret: setRotatedSecret },
      {
        onSuccess: () => {
          toast({ tone: "success", title: t("webhooks.rotateDialog.done") });
          setConfirming(null);
        },
        onError: () => toast({ tone: "danger", title: t("webhooks.rotateDialog.failed") }),
      },
    );
  }

  function doEnable() {
    enable.mutate(undefined, {
      onSuccess: () => toast({ tone: "success", title: t("webhooks.enableDialog.done"), description: endpoint.url }),
      onError: () => toast({ tone: "danger", title: t("webhooks.enableDialog.failed") }),
    });
  }

  function doTest() {
    sendTest.mutate(undefined, {
      onSuccess: (result) => {
        setTestEventId(result.event_id);
        toast({ tone: "success", title: t("webhooks.test.sent"), description: t("webhooks.test.sentDescription") });
      },
      onError: () => toast({ tone: "danger", title: t("webhooks.test.failed") }),
    });
  }

  return (
    <>
      <BackLink />
      <PageHeader
        title={t("webhooks.detail.title")}
        description={endpoint.url}
        actions={
          <div className="flex flex-wrap gap-2" role="group" aria-label={t("webhooks.detail.actions")}>
            <GatedButton variant="secondary" allowed={mayManage} reason={reason} onClick={() => setEditing(true)}>
              {t("webhooks.detail.edit")}
            </GatedButton>
            <GatedButton variant="secondary" allowed={mayManage} reason={reason} onClick={() => setConfirming("rotate")}>
              {t("webhooks.detail.rotate")}
            </GatedButton>
            {endpoint.enabled ? (
              <GatedButton variant="secondary" allowed={mayManage} reason={reason} onClick={() => setConfirming("disable")}>
                {t("webhooks.detail.disable")}
              </GatedButton>
            ) : (
              <GatedButton variant="secondary" allowed={mayManage} reason={reason} disabled={enable.isPending} onClick={doEnable}>
                {t("webhooks.detail.enable")}
              </GatedButton>
            )}
            <GatedButton allowed={mayManage} reason={reason} disabled={sendTest.isPending} onClick={doTest}>
              <Send aria-hidden="true" />
              {sendTest.isPending ? t("webhooks.detail.testSending") : t("webhooks.detail.test")}
            </GatedButton>
          </div>
        }
      />

      {testEventId ? (
        <p role="status" className="mb-4 flex flex-wrap items-center gap-2 rounded-md border border-line bg-surface-sunken px-3 py-2 text-sm">
          <span>{t("webhooks.test.sent")}</span>
          <Link to={EVENT_PATH} params={{ eventId: testEventId }} className="rounded-xs text-primary underline-offset-2 hover:underline">
            {t("webhooks.test.view")}
          </Link>
          <code className="font-mono text-xs text-ink-muted">{testEventId}</code>
        </p>
      ) : null}

      <div className="flex flex-col gap-4">
        <Card>
          <CardHeader>
            <CardTitle>{t("webhooks.detail.configuration")}</CardTitle>
            <CardDescription>{t("webhooks.detail.configurationDescription")}</CardDescription>
          </CardHeader>
          <CardContent>
            <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
              <Fact label={t("webhooks.detail.facts.url")}>
                <span className="font-mono text-xs break-all">{endpoint.url}</span>
              </Fact>
              <Fact label={t("webhooks.detail.facts.description")}>
                {endpoint.description ?? <span className="text-ink-faint">{t("webhooks.noDescription")}</span>}
              </Fact>
              <Fact label={t("webhooks.detail.facts.status")}>
                <EndpointStatusBadge enabled={endpoint.enabled} />
              </Fact>
              <Fact label={t("webhooks.detail.facts.events")}>
                <EventFilterChips eventTypes={endpoint.event_types} max={50} />
              </Fact>
              <Fact label={t("webhooks.detail.facts.created")}>
                <DateTime value={endpoint.created_at} />
              </Fact>
              <Fact label={t("webhooks.detail.facts.updated")}>
                <DateTime value={endpoint.updated_at} />
              </Fact>
              {endpoint.secret_rotation_ends_at ? (
                <Fact label={t("webhooks.detail.facts.rotation")}>
                  <DateTime value={endpoint.secret_rotation_ends_at} />
                </Fact>
              ) : null}
            </dl>
          </CardContent>
        </Card>

        <AttemptsCard projectId={projectId} endpointId={endpoint.id} />
      </div>

      <EditEndpointDialog projectId={projectId} endpoint={endpoint} open={editing} onOpenChange={setEditing} />
      <ConfirmDialog
        open={confirming === "disable"}
        onOpenChange={(open) => (open ? undefined : setConfirming(null))}
        title={t("webhooks.disableDialog.title")}
        description={t("webhooks.disableDialog.description", { url: endpoint.url })}
        confirmLabel={t("webhooks.disableDialog.confirm")}
        tone="danger"
        pending={disable.isPending}
        error={disable.isError ? errorText(disable.error) : null}
        onConfirm={confirmDisable}
      />
      <ConfirmDialog
        open={confirming === "rotate"}
        onOpenChange={(open) => (open ? undefined : setConfirming(null))}
        title={t("webhooks.rotateDialog.title")}
        description={t("webhooks.rotateDialog.description")}
        confirmLabel={t("webhooks.rotateDialog.confirm")}
        tone="primary"
        pending={rotate.isPending}
        error={rotate.isError ? errorText(rotate.error) : null}
        onConfirm={confirmRotate}
      />
      <RotatedSecretDialog secret={rotatedSecret} url={endpoint.url} onDone={() => setRotatedSecret(null)} />
    </>
  );
}

function AttemptsCard({ projectId, endpointId }: { projectId: string; endpointId: string }) {
  const { t } = useI18n();
  const attempts = useWebhookAttempts(projectId, endpointId);
  let body: ReactNode;
  if (attempts.isPending) {
    body = <ListSkeleton rows={3} label={t("webhooks.attempts.loading")} />;
  } else if (attempts.isError) {
    body = <QueryErrorState title={t("webhooks.attempts.loadFailed")} error={attempts.error} onRetry={() => void attempts.refetch()} />;
  } else if (attempts.data.data.length === 0) {
    body = <p className="px-5 py-4 text-sm text-ink-muted">{t("webhooks.attempts.none")}</p>;
  } else {
    body = (
      <div className="overflow-x-auto">
        <Table>
          <caption className="sr-only">{t("webhooks.attempts.caption")}</caption>
          <TableHeader>
            <TableRow className="hover:bg-transparent">
              <TableHead className="text-right">{t("webhooks.attempts.cols.attempt")}</TableHead>
              <TableHead>{t("webhooks.attempts.cols.outcome")}</TableHead>
              <TableHead className="text-right">{t("webhooks.attempts.cols.status")}</TableHead>
              <TableHead className="text-right">{t("webhooks.attempts.cols.latency")}</TableHead>
              <TableHead>{t("webhooks.attempts.cols.error")}</TableHead>
              <TableHead>{t("webhooks.attempts.cols.time")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {attempts.data.data.map((attempt) => (
              <TableRow key={attempt.id}>
                <TableCell className="figures text-right">{attempt.attempt}</TableCell>
                <TableCell>
                  <AttemptOutcomeBadge outcome={attempt.outcome} />
                </TableCell>
                <TableCell className="figures text-right">{attempt.status_code ?? <span className="text-ink-faint">—</span>}</TableCell>
                <TableCell className="figures text-right">{t("webhooks.attempts.latency", { ms: attempt.latency_ms })}</TableCell>
                <TableCell className="max-w-64 text-xs">
                  {attempt.error ? (
                    <span className="block truncate text-danger" title={attempt.error}>
                      {attempt.error}
                    </span>
                  ) : (
                    <span className="text-ink-faint">{t("webhooks.attempts.noError")}</span>
                  )}
                </TableCell>
                <TableCell>
                  <DateTime value={attempt.attempted_at} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    );
  }
  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-3">
        <div>
          <CardTitle>{t("webhooks.attempts.title")}</CardTitle>
          <CardDescription>{t("webhooks.attempts.description")}</CardDescription>
        </div>
        <Button variant="secondary" size="sm" disabled={attempts.isFetching} onClick={() => void attempts.refetch()}>
          <RefreshCw aria-hidden="true" className={attempts.isFetching ? "animate-spin" : undefined} />
          {attempts.isFetching ? t("webhooks.attempts.refreshing") : t("webhooks.attempts.refresh")}
        </Button>
      </CardHeader>
      <CardContent className="px-0 py-0">{body}</CardContent>
    </Card>
  );
}
