import { ChevronDown, ChevronRight, ScrollText, Search, X } from "lucide-react";
import { Fragment, useState } from "react";

import type { AuditRecordOut } from "../api/generated/model";
import { DateTime } from "../components/date-time";
import { PageHeader } from "../components/page-header";
import { Button } from "../components/ui/button";
import { EmptyState } from "../components/ui/empty-state";
import { Input } from "../components/ui/input";
import { Select } from "../components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { flattenAudit, useAuditRecords } from "../features/audit/audit-list-api";
import { isEmptyObject, metadataJson } from "../features/audit/redaction";
import { describeActivity, KNOWN_ACTIONS, type OperatorNames } from "../features/console/audit-activity";
import { useMembers } from "../features/console/members-api";
import { NoProjectState } from "../features/console/no-project";
import { deniedReason } from "../features/console/permissions";
import { ListSkeleton, QueryErrorState } from "../features/console/states";
import { useSession } from "../features/auth/use-session";
import { useProject } from "../features/console/use-project";
import { useDocumentTitle } from "../features/shell/document-title";
import { useI18n } from "../i18n";

export function AuditPage() {
  const { t } = useI18n();
  const { membership, can } = useProject();
  useDocumentTitle(t("audit.title"));
  if (!membership) {
    return (
      <>
        <PageHeader title={t("audit.title")} description={t("audit.description")} />
        <NoProjectState />
      </>
    );
  }
  if (!can("audit:read")) {
    return (
      <>
        <PageHeader title={t("audit.title")} description={t("audit.description")} />
        <EmptyState icon={ScrollText} title={t("audit.noAccess")} description={deniedReason("audit:read", membership.role)} />
      </>
    );
  }
  return <AuditForProject projectId={membership.project_id} mayListMembers={can("members:read")} />;
}

function AuditForProject({ projectId, mayListMembers }: { projectId: string; mayListMembers: boolean }) {
  const { t } = useI18n();
  const { operator } = useSession();
  const members = useMembers(mayListMembers ? projectId : null);
  const [text, setText] = useState("");
  const [action, setAction] = useState("");
  const [actorId, setActorId] = useState("");
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const audit = useAuditRecords(projectId, { action, actorId });
  const rows = flattenAudit(audit.data);

  const people: [string, string][] = [
    [operator.id, operator.display_name],
    ...(members.data?.data ?? []).map((member): [string, string] => [member.operator_id, member.display_name]),
  ];
  const names: OperatorNames = new Map(people);
  const actors = [...names.entries()];

  const needle = text.trim().toLowerCase();
  const described = rows.map((record) => ({ record, line: describeActivity(record, names) }));
  const visible = described.filter(({ record, line }) => {
    if (needle === "") return true;
    return [line.sentence, line.actor, record.action, record.target_type, record.target_id ?? "", record.actor_id ?? ""]
      .join(" ")
      .toLowerCase()
      .includes(needle);
  });
  const filtering = text !== "" || action !== "" || actorId !== "";
  const clear = () => {
    setText("");
    setAction("");
    setActorId("");
  };
  const toggle = (id: string) =>
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  let body;
  if (audit.isPending) {
    body = <ListSkeleton label={t("audit.loading")} />;
  } else if (audit.isError) {
    body = <QueryErrorState title={t("audit.loadFailed")} error={audit.error} onRetry={() => void audit.refetch()} />;
  } else if (rows.length === 0 && !filtering) {
    body = <EmptyState icon={ScrollText} title={t("audit.emptyTitle")} description={t("audit.emptyDescription")} />;
  } else {
    body = (
      <>
        {visible.length === 0 ? (
          <EmptyState
            icon={Search}
            title={t("audit.filters.noMatchTitle")}
            description={t("audit.filters.noMatchDescription")}
            action={
              <Button variant="secondary" onClick={clear}>
                {t("audit.filters.clear")}
              </Button>
            }
          />
        ) : (
          <div className="rounded-md border border-line bg-surface">
            <Table>
              <caption className="sr-only">{t("audit.caption")}</caption>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>{t("audit.cols.action")}</TableHead>
                  <TableHead>{t("audit.cols.actor")}</TableHead>
                  <TableHead>{t("audit.cols.target")}</TableHead>
                  <TableHead>{t("audit.cols.time")}</TableHead>
                  <TableHead>
                    <span className="sr-only">{t("audit.cols.details")}</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visible.map(({ record, line }) => (
                  <AuditRow
                    key={record.id}
                    record={record}
                    sentence={line.sentence}
                    actor={line.actor}
                    expanded={open.has(record.id)}
                    onToggle={() => toggle(record.id)}
                  />
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-ink-muted" aria-live="polite">
            {t(audit.hasNextPage ? "audit.loadedMore" : "audit.loaded", { count: rows.length })}
            {text !== "" ? ` · ${t("audit.filters.scopeNote")}` : null}
          </p>
          {audit.hasNextPage ? (
            <Button variant="secondary" onClick={() => void audit.fetchNextPage()} disabled={audit.isFetchingNextPage}>
              {audit.isFetchingNextPage ? t("audit.loadingMore") : t("audit.loadMore")}
            </Button>
          ) : null}
        </div>
        {audit.isFetchNextPageError ? (
          <p role="alert" className="mt-2 text-sm text-danger">
            {t("audit.nextPageFailed")}
          </p>
        ) : null}
      </>
    );
  }

  return (
    <>
      <PageHeader title={t("audit.title")} description={t("audit.description")} />
      <form role="search" aria-label={t("audit.filters.label")} onSubmit={(event) => event.preventDefault()} className="mb-4 flex flex-wrap items-end gap-2">
        <div className="min-w-56 flex-1">
          <label htmlFor="audit-filter-text" className="mb-1 block text-sm font-medium">
            {t("audit.filters.search")}
          </label>
          <Input
            id="audit-filter-text"
            value={text}
            onChange={(event) => setText(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            placeholder={t("audit.filters.searchPlaceholder")}
          />
        </div>
        <div className="w-full sm:w-56">
          <label htmlFor="audit-filter-action" className="mb-1 block text-sm font-medium">
            {t("audit.filters.action")}
          </label>
          <Select id="audit-filter-action" value={action} onChange={(event) => setAction(event.target.value)}>
            <option value="">{t("audit.filters.allActions")}</option>
            {KNOWN_ACTIONS.map((code) => (
              <option key={code} value={code}>
                {actionLabel(code)}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-full sm:w-48">
          <label htmlFor="audit-filter-actor" className="mb-1 block text-sm font-medium">
            {t("audit.filters.actor")}
          </label>
          <Select id="audit-filter-actor" value={actorId} onChange={(event) => setActorId(event.target.value)}>
            <option value="">{t("audit.filters.allActors")}</option>
            {actors.map(([id, name]) => (
              <option key={id} value={id}>
                {name}
              </option>
            ))}
          </Select>
        </div>
        {filtering ? (
          <Button type="button" variant="ghost" onClick={clear}>
            <X aria-hidden="true" />
            {t("audit.filters.clear")}
          </Button>
        ) : null}
      </form>
      {body}
    </>
  );
}

/** The action dropdown shows the same humanized sentence people see in the list. */
function actionLabel(code: string): string {
  const probe: AuditRecordOut = {
    id: code,
    action: code,
    actor_id: null,
    actor_type: "system",
    correlation_id: null,
    diff: {},
    ip: null,
    occurred_at: "",
    project_id: null,
    request_id: null,
    target_id: null,
    target_type: "",
    user_agent: null,
  };
  return describeActivity(probe, new Map()).sentence;
}

function AuditRow({
  record,
  sentence,
  actor,
  expanded,
  onToggle,
}: {
  record: AuditRecordOut;
  sentence: string;
  actor: string;
  expanded: boolean;
  onToggle: () => void;
}) {
  const { t } = useI18n();
  const detailId = `audit-detail-${record.id}`;
  const Chevron = expanded ? ChevronDown : ChevronRight;
  return (
    <Fragment>
      <TableRow>
        <TableCell>
          <div className="flex flex-col gap-0.5">
            <span className="font-medium">{sentence}</span>
            <span className="font-mono text-xs text-ink-muted">{record.action}</span>
          </div>
        </TableCell>
        <TableCell>{actor}</TableCell>
        <TableCell>
          <div className="flex flex-col gap-0.5">
            <span className="text-xs text-ink-muted">{record.target_type}</span>
            {record.target_id ? (
              <span className="block max-w-[14rem] truncate font-mono text-xs" title={record.target_id}>
                {record.target_id}
              </span>
            ) : null}
          </div>
        </TableCell>
        <TableCell className="text-ink-muted">
          <DateTime value={record.occurred_at} />
        </TableCell>
        <TableCell className="text-right">
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={detailId}
            aria-label={t(expanded ? "audit.collapse" : "audit.expand", { action: sentence })}
            onClick={onToggle}
            className="grid size-8 place-items-center rounded-xs text-ink-muted hover:bg-surface-sunken hover:text-ink"
          >
            <Chevron className="size-4" aria-hidden="true" />
          </button>
        </TableCell>
      </TableRow>
      {expanded ? (
        <TableRow id={detailId} className="bg-surface-sunken hover:bg-surface-sunken">
          <TableCell colSpan={5}>
            <AuditDetail record={record} />
          </TableCell>
        </TableRow>
      ) : null}
    </Fragment>
  );
}

function AuditDetail({ record }: { record: AuditRecordOut }) {
  const { t } = useI18n();
  const facts: [string, string | null][] = [
    [t("audit.detail.id"), record.id],
    [t("audit.detail.requestId"), record.request_id],
    [t("audit.detail.correlationId"), record.correlation_id],
    [t("audit.detail.ip"), record.ip],
    [t("audit.detail.userAgent"), record.user_agent],
  ];
  return (
    <div className="grid gap-4 py-1 lg:grid-cols-2">
      <dl className="grid gap-x-6 gap-y-2 sm:grid-cols-2">
        {facts
          .filter(([, value]) => value !== null && value !== "")
          .map(([label, value]) => (
            <div key={label} className="min-w-0">
              <dt className="text-xs text-ink-muted">{label}</dt>
              <dd className="mt-0.5 font-mono text-xs break-all">{value}</dd>
            </div>
          ))}
      </dl>
      <div className="min-w-0">
        <h3 className="mb-1 text-xs font-semibold text-ink-muted">{t("audit.detail.metadata")}</h3>
        {isEmptyObject(record.diff) ? (
          <p className="text-sm text-ink-muted">{t("audit.detail.noMetadata")}</p>
        ) : (
          <pre className="max-h-64 overflow-auto rounded-sm bg-surface p-3 font-mono text-xs">{metadataJson(record.diff, t("audit.detail.hiddenField"))}</pre>
        )}
      </div>
    </div>
  );
}
