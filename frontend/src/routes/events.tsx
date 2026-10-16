import { Link } from "@tanstack/react-router";
import { Activity, Radio, X } from "lucide-react";
import { useState } from "react";

import type { EventOut } from "../api/generated/model";
import { DateTime } from "../components/date-time";
import { PageHeader } from "../components/page-header";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { EmptyState } from "../components/ui/empty-state";
import { Select } from "../components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { NoProjectState } from "../features/console/no-project";
import { deniedReason } from "../features/console/permissions";
import { ListSkeleton, QueryErrorState } from "../features/console/states";
import { useProject } from "../features/console/use-project";
import { CopyableValue, EventTypeLabel } from "../features/events/event-parts";
import { EVENT_TYPE_OPTIONS } from "../features/events/event-types";
import { flattenEvents, useEvents } from "../features/events/events-api";
import { useDocumentTitle } from "../features/shell/document-title";
import { useI18n } from "../i18n";
import { cn } from "../lib/cn";

// Screens registered from the SCREENS table are not in the router's static route types, so these paths are plain strings.
const EVENT_PATH: string = "/events/$eventId";

export function EventsPage() {
  const { t } = useI18n();
  const { membership, can } = useProject();
  useDocumentTitle(t("events.title"));
  if (!membership) {
    return (
      <>
        <PageHeader title={t("events.title")} description={t("events.description")} />
        <NoProjectState />
      </>
    );
  }
  if (!can("events:read")) {
    return (
      <>
        <PageHeader title={t("events.title")} description={t("events.description")} />
        <EmptyState icon={Activity} title={t("events.noAccess")} description={deniedReason("events:read", membership.role)} />
      </>
    );
  }
  return <EventsForProject projectId={membership.project_id} />;
}

interface Seen {
  key: string;
  head: string | null;
  ids: ReadonlySet<string>;
}

/**
 * Which rows arrived after the list was first shown: the ones above the previous newest row. The first load of a
 * filter is the baseline (nothing is "new"), and older pages loaded later never count. Derived during render.
 */
function useFreshIds(key: string, rows: readonly EventOut[], loaded: boolean): ReadonlySet<string> {
  const [seen, setSeen] = useState<Seen | null>(null);
  const [fresh, setFresh] = useState<ReadonlySet<string>>(new Set());
  if (!loaded) return fresh;
  if (seen === null || seen.key !== key) {
    setSeen({ key, head: rows[0]?.id ?? null, ids: new Set(rows.map((row) => row.id)) });
    setFresh(new Set());
    return fresh;
  }
  const unseen = rows.filter((row) => !seen.ids.has(row.id));
  if (unseen.length === 0) return fresh;
  const headIndex = seen.head === null ? rows.length : rows.findIndex((row) => row.id === seen.head);
  const arrivals = headIndex < 0 ? [] : rows.slice(0, headIndex).filter((row) => !seen.ids.has(row.id));
  setSeen({ key, head: rows[0]?.id ?? seen.head, ids: new Set(rows.map((row) => row.id)) });
  if (arrivals.length > 0) setFresh(new Set([...fresh, ...arrivals.map((row) => row.id)]));
  return fresh;
}

function EventsForProject({ projectId }: { projectId: string }) {
  const { t, tLoose } = useI18n();
  const { stream } = useProject();
  const optionLabel = (option: string) => {
    const label = tLoose("events.types", option, "");
    return label ? `${label} · ${option}` : option;
  };
  const [type, setType] = useState("");
  const events = useEvents(projectId, type);
  const rows = flattenEvents(events.data);
  const fresh = useFreshIds(`${projectId}|${type}`, rows, events.isSuccess);

  const filterBar = (
    <form role="search" aria-label={t("events.filters.label")} onSubmit={(event) => event.preventDefault()} className="mb-4 flex flex-wrap items-end gap-2">
      <div className="w-full sm:w-72">
        <label htmlFor="event-filter-type" className="mb-1 block text-sm font-medium">
          {t("events.filters.type")}
        </label>
        <Select id="event-filter-type" value={type} onChange={(event) => setType(event.target.value)}>
          <option value="">{t("events.filters.allTypes")}</option>
          {EVENT_TYPE_OPTIONS.map((option) => (
            <option key={option} value={option}>
              {optionLabel(option)}
            </option>
          ))}
        </Select>
      </div>
      {type !== "" ? (
        <Button type="button" variant="ghost" onClick={() => setType("")}>
          <X aria-hidden="true" />
          {t("payments.filters.clear")}
        </Button>
      ) : null}
      <p className="ml-auto flex items-center gap-1.5 pb-2 text-xs text-ink-muted">
        <Radio className={cn("size-3.5", stream.status === "open" ? "text-success" : "text-ink-faint")} aria-hidden="true" />
        {t("events.live")}
      </p>
    </form>
  );

  let body;
  if (events.isPending) {
    body = <ListSkeleton label={t("events.loading")} />;
  } else if (events.isError) {
    body = <QueryErrorState title={t("events.loadFailed")} error={events.error} onRetry={() => void events.refetch()} />;
  } else if (rows.length === 0) {
    body = (
      <EmptyState
        icon={Activity}
        title={t(type ? "events.emptyFilteredTitle" : "events.emptyTitle")}
        description={t(type ? "events.emptyFilteredDescription" : "events.emptyDescription")}
      />
    );
  } else {
    body = (
      <>
        <div className="rounded-md border border-line bg-surface">
          <Table>
            <caption className="sr-only">{t("events.caption")}</caption>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>{t("events.cols.type")}</TableHead>
                <TableHead>{t("events.cols.subject")}</TableHead>
                <TableHead>{t("events.cols.time")}</TableHead>
                <TableHead>{t("events.cols.correlation")}</TableHead>
                <TableHead>{t("events.cols.version")}</TableHead>
                <TableHead>
                  <span className="sr-only">{t("events.cols.actions")}</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((event) => (
                <EventRow key={event.id} event={event} isNew={fresh.has(event.id)} />
              ))}
            </TableBody>
          </Table>
        </div>
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-ink-muted" aria-live="polite">
            {t(events.hasNextPage ? "events.loadedMore" : "events.loaded", { count: rows.length })}
          </p>
          {events.hasNextPage ? (
            <Button variant="secondary" onClick={() => void events.fetchNextPage()} disabled={events.isFetchingNextPage}>
              {events.isFetchingNextPage ? t("events.loadingMore") : t("events.loadMore")}
            </Button>
          ) : null}
        </div>
        {events.isFetchNextPageError ? (
          <p role="alert" className="mt-2 text-sm text-danger">
            {t("events.nextPageFailed")}
          </p>
        ) : null}
      </>
    );
  }

  return (
    <>
      <PageHeader title={t("events.title")} description={t("events.description")} />
      {filterBar}
      {body}
    </>
  );
}

function EventRow({ event, isNew }: { event: EventOut; isNew: boolean }) {
  const { t } = useI18n();
  return (
    <TableRow className={cn(isNew && "animate-arrive")} data-new={isNew ? "true" : undefined}>
      <TableCell>
        <div className="flex flex-wrap items-center gap-2">
          <EventTypeLabel type={event.type} />
          {isNew ? <Badge tone="primary">{t("events.newEvent")}</Badge> : null}
        </div>
      </TableCell>
      <TableCell className="font-mono text-xs break-all">{event.subject ?? <span className="text-ink-faint">—</span>}</TableCell>
      <TableCell className="text-ink-muted">
        <DateTime value={event.occurred_at} />
      </TableCell>
      <TableCell>
        {event.correlation_id ? <CopyableValue value={event.correlation_id} label={t("events.detail.facts.correlation")} /> : <span className="text-ink-faint">—</span>}
      </TableCell>
      <TableCell className="figures text-ink-muted">v{event.version}</TableCell>
      <TableCell className="text-right">
        <Link
          to={EVENT_PATH}
          params={{ eventId: event.id }}
          aria-label={t("events.openNamed", { id: event.id })}
          className="rounded-xs text-sm font-medium text-primary underline-offset-2 hover:underline"
        >
          {t("events.open")}
        </Link>
      </TableCell>
    </TableRow>
  );
}
