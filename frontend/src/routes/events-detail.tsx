import { Link, useParams } from "@tanstack/react-router";
import { Activity, ArrowLeft } from "lucide-react";
import type { ReactNode } from "react";

import type { CausalityNodeOut, CausalityOut, DeliveryOut, EventOut } from "../api/generated/model";
import { DateTime } from "../components/date-time";
import { PageHeader } from "../components/page-header";
import { Badge } from "../components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { EmptyState } from "../components/ui/empty-state";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { NoProjectState } from "../features/console/no-project";
import { deniedReason } from "../features/console/permissions";
import { ListSkeleton, QueryErrorState } from "../features/console/states";
import { useProject } from "../features/console/use-project";
import { CopyableValue, DeliveryStatusBadge, EventTypeLabel, JsonBlock } from "../features/events/event-parts";
import { useCausality, useEvent } from "../features/events/events-api";
import { useDocumentTitle } from "../features/shell/document-title";
import { useI18n } from "../i18n";
import { isApiError } from "../lib/api";
import { cn } from "../lib/cn";

// Routes registered from the SCREENS table are not in the router's static route types, so these paths are plain strings.
const EVENTS_PATH: string = "/events";
const EVENT_PATH: string = "/events/$eventId";

export function EventDetailPage() {
  const { t } = useI18n();
  const { membership, can } = useProject();
  const { eventId } = useParams({ strict: false });
  useDocumentTitle(t("events.detail.title"));
  if (!membership) return <NoProjectState />;
  if (!can("events:read")) {
    return <EmptyState icon={Activity} title={t("events.noAccess")} description={deniedReason("events:read", membership.role)} />;
  }
  if (!eventId) return null;
  return <EventDetail projectId={membership.project_id} eventId={eventId} />;
}

function BackLink() {
  const { t } = useI18n();
  return (
    <Link to={EVENTS_PATH} className="mb-3 inline-flex items-center gap-1 rounded-xs text-sm text-primary underline-offset-2 hover:underline">
      <ArrowLeft className="size-4" aria-hidden="true" />
      {t("events.detail.back")}
    </Link>
  );
}

function EventDetail({ projectId, eventId }: { projectId: string; eventId: string }) {
  const { t } = useI18n();
  const query = useEvent(projectId, eventId);
  if (query.isPending) {
    return (
      <>
        <BackLink />
        <ListSkeleton label={t("events.detail.loading")} />
      </>
    );
  }
  if (query.isError) {
    if (isApiError(query.error) && query.error.status === 404) {
      return (
        <>
          <BackLink />
          <EmptyState icon={Activity} title={t("events.detail.notFoundTitle")} description={t("events.detail.notFoundDescription")} />
        </>
      );
    }
    return (
      <>
        <BackLink />
        <QueryErrorState title={t("events.detail.loadFailed")} error={query.error} onRetry={() => void query.refetch()} />
      </>
    );
  }
  return <EventView projectId={projectId} event={query.data.event} deliveries={query.data.deliveries} />;
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium tracking-wide text-ink-muted uppercase">{label}</dt>
      <dd className="mt-0.5 text-sm">{children}</dd>
    </div>
  );
}

function EventView({ projectId, event, deliveries }: { projectId: string; event: EventOut; deliveries: DeliveryOut[] }) {
  const { t } = useI18n();
  return (
    <>
      <BackLink />
      <PageHeader title={t("events.detail.title")} description={event.subject ?? undefined} />

      <div className="mb-6 flex flex-wrap items-center gap-3">
        <EventTypeLabel type={event.type} />
        <CopyableValue value={event.id} label="id" truncate={false} />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <div className="flex flex-col gap-4 xl:col-span-2">
          <Card>
            <CardHeader>
              <CardTitle>{t("events.detail.envelope")}</CardTitle>
              <CardDescription>{t("events.detail.envelopeCaption")}</CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
                <Fact label={t("events.detail.facts.occurred")}>
                  <DateTime value={event.occurred_at} />
                </Fact>
                <Fact label={t("events.detail.facts.recorded")}>
                  <DateTime value={event.recorded_at} />
                </Fact>
                <Fact label={t("events.detail.facts.version")}>
                  <span className="figures">v{event.version}</span>
                </Fact>
                <Fact label={t("events.detail.facts.correlation")}>
                  {event.correlation_id ? <CopyableValue value={event.correlation_id} label={t("events.detail.facts.correlation")} /> : <span className="text-ink-faint">—</span>}
                </Fact>
                <Fact label={t("events.detail.facts.causation")}>
                  {event.causation_id ? <CopyableValue value={event.causation_id} label={t("events.detail.facts.causation")} /> : <span className="text-ink-faint">—</span>}
                </Fact>
                <Fact label={t("events.detail.facts.transaction")}>
                  <span className="figures font-mono text-xs">{event.txid}</span>
                </Fact>
              </dl>
              <JsonBlock value={event} label={t("events.detail.envelope")} />
            </CardContent>
          </Card>

          <DeliveriesCard deliveries={deliveries} />
        </div>

        <CausalityCard projectId={projectId} eventId={event.id} />
      </div>
    </>
  );
}

function DeliveriesCard({ deliveries }: { deliveries: DeliveryOut[] }) {
  const { t } = useI18n();
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("events.detail.deliveries")}</CardTitle>
        <CardDescription>{t("events.detail.deliveriesDescription")}</CardDescription>
      </CardHeader>
      <CardContent className="px-0 py-0">
        {deliveries.length === 0 ? (
          <p className="px-5 py-4 text-sm text-ink-muted">{t("events.detail.deliveriesNone")}</p>
        ) : (
          <Table>
            <caption className="sr-only">{t("events.detail.deliveriesCaption")}</caption>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>{t("events.detail.deliveryCols.subscription")}</TableHead>
                <TableHead>{t("events.detail.deliveryCols.status")}</TableHead>
                <TableHead className="text-right">{t("events.detail.deliveryCols.attempts")}</TableHead>
                <TableHead>{t("events.detail.deliveryCols.lastError")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {deliveries.map((delivery) => (
                <TableRow key={delivery.id}>
                  <TableCell>
                    <div className="flex flex-col gap-0.5">
                      <span className="font-mono text-xs break-all">{delivery.subscription_id ?? delivery.handler}</span>
                      <span className="text-xs text-ink-muted">
                        {delivery.kind} · {delivery.handler}
                      </span>
                    </div>
                  </TableCell>
                  <TableCell>
                    <DeliveryStatusBadge status={delivery.status} />
                  </TableCell>
                  <TableCell className="figures text-right">{delivery.attempts}</TableCell>
                  <TableCell className="max-w-64 text-xs break-words">
                    {delivery.last_error ? <span className="text-danger">{delivery.last_error}</span> : <span className="text-ink-faint">{t("events.detail.noError")}</span>}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

function CausalityCard({ projectId, eventId }: { projectId: string; eventId: string }) {
  const { t } = useI18n();
  const query = useCausality(projectId, eventId);
  let body;
  if (query.isPending) {
    body = <ListSkeleton rows={3} label={t("events.detail.causalityLoading")} />;
  } else if (query.isError) {
    body = <QueryErrorState title={t("events.detail.causalityFailed")} error={query.error} onRetry={() => void query.refetch()} />;
  } else {
    body = <CausalityTimeline data={query.data} />;
  }
  return (
    <Card className="h-fit">
      <CardHeader>
        <CardTitle>{t("events.detail.causality")}</CardTitle>
        <CardDescription>{t("events.detail.causalityDescription")}</CardDescription>
      </CardHeader>
      <CardContent>{body}</CardContent>
    </Card>
  );
}

interface Step {
  event: EventOut;
  depth: number;
  role: "ancestor" | "current" | "descendant";
}

function flattenChildren(nodes: readonly CausalityNodeOut[], depth: number): Step[] {
  return nodes.flatMap((node) => [{ event: node.event, depth, role: "descendant" as const }, ...flattenChildren(node.children, depth + 1)]);
}

/** Oldest cause first, then this event, then what it triggered (indented by generation). */
function CausalityTimeline({ data }: { data: CausalityOut }) {
  const { t } = useI18n();
  const ancestors = [...data.ancestors].reverse();
  const steps: Step[] = [
    ...ancestors.map((event, index) => ({ event, depth: index, role: "ancestor" as const })),
    { event: data.root, depth: ancestors.length, role: "current" as const },
    ...flattenChildren(data.descendants, ancestors.length + 1),
  ];
  if (steps.length === 1) {
    return <p className="text-sm text-ink-muted">{t("events.detail.causalityNone")}</p>;
  }
  return (
    <div className="flex flex-col gap-3">
      <ol className="relative flex flex-col gap-3 border-l border-line-strong pl-4" aria-label={t("events.detail.causality")}>
        {steps.map((step) => (
          <TimelineItem key={`${step.role}-${step.event.id}`} step={step} />
        ))}
      </ol>
      {data.journal_entries.length > 0 ? (
        <div>
          <h3 className="mb-1 text-xs font-medium tracking-wide text-ink-muted uppercase">{t("events.detail.journal")}</h3>
          <ul className="flex flex-col gap-1">
            {data.journal_entries.map((entry) => (
              <li key={entry.id} className="flex flex-wrap items-center gap-2 text-xs">
                <code className="font-mono">{entry.recipe}</code>
                <Badge tone={entry.status === "posted" ? "success" : "neutral"}>{entry.status}</Badge>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {data.truncated ? <p className="text-xs text-ink-muted">{t("events.detail.truncated")}</p> : null}
    </div>
  );
}

function TimelineItem({ step }: { step: Step }) {
  const { t } = useI18n();
  const current = step.role === "current";
  const group = step.role === "ancestor" ? t("events.detail.ancestors") : step.role === "descendant" ? t("events.detail.descendants") : t("events.detail.current");
  return (
    <li className={cn("relative")} style={{ marginLeft: `${Math.min(step.depth, 4) * 0.75}rem` }} aria-current={current ? "step" : undefined}>
      <span
        className={cn("absolute top-1.5 -left-[1.3rem] size-2.5 rounded-full border-2 border-surface", current ? "bg-primary ring-2 ring-primary-soft" : "bg-line-strong")}
        aria-hidden="true"
      />
      <p className="text-xs text-ink-muted">{group}</p>
      <div className="flex flex-wrap items-baseline gap-x-2">
        {current ? (
          <EventTypeLabel type={step.event.type} />
        ) : (
          <Link
            to={EVENT_PATH}
            params={{ eventId: step.event.id }}
            aria-label={t("events.openNamed", { id: step.event.id })}
            className="rounded-xs text-primary underline-offset-2 hover:underline"
          >
            <EventTypeLabel type={step.event.type} />
          </Link>
        )}
      </div>
      {step.event.subject ? <p className="font-mono text-xs break-all text-ink-muted">{step.event.subject}</p> : null}
      <DateTime value={step.event.occurred_at} className="text-xs text-ink-muted" />
    </li>
  );
}
