import { Link, useParams } from "@tanstack/react-router";
import { ArrowLeft, BookOpenText } from "lucide-react";

import type { EntryDetailOut } from "../api/generated/model";
import { DateTime } from "../components/date-time";
import { PageHeader } from "../components/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { EmptyState } from "../components/ui/empty-state";
import { isEmptyObject, metadataJson } from "../features/audit/redaction";
import { NoProjectState } from "../features/console/no-project";
import { deniedReason } from "../features/console/permissions";
import { ListSkeleton, QueryErrorState } from "../features/console/states";
import { useProject } from "../features/console/use-project";
import { EntryLink, EntryStatusBadge, Fact, PostingsTable, RecipeLabel } from "../features/ledger/entry-parts";
import { LEDGER_PATH } from "../features/ledger/ledger-labels";
import { useEntry } from "../features/ledger/ledger-api";
import { paymentIdOf } from "../features/ledger/ledger-model";
import { CopyableId } from "../features/payments/payment-parts";
import { useDocumentTitle } from "../features/shell/document-title";
import { useI18n } from "../i18n";
import { isApiError } from "../lib/api";

export function LedgerEntryPage() {
  const { t } = useI18n();
  const { membership, can } = useProject();
  const { entryId } = useParams({ strict: false });
  useDocumentTitle(t("ledger.title"));
  if (!membership) return <NoProjectState />;
  if (!can("ledger:read")) {
    return <EmptyState icon={BookOpenText} title={t("ledger.noAccess")} description={deniedReason("ledger:read", membership.role)} />;
  }
  if (!entryId) return null;
  return <EntryDetail projectId={membership.project_id} entryId={entryId} />;
}

function BackLink() {
  const { t } = useI18n();
  return (
    <Link
      to={LEDGER_PATH}
      search={{ tab: "entries" }}
      className="mb-3 inline-flex items-center gap-1 rounded-xs text-sm text-primary underline-offset-2 hover:underline"
    >
      <ArrowLeft className="size-4" aria-hidden="true" />
      {t("ledger.detail.back")}
    </Link>
  );
}

function EntryDetail({ projectId, entryId }: { projectId: string; entryId: string }) {
  const { t } = useI18n();
  const query = useEntry(projectId, entryId);
  if (query.isPending) {
    return (
      <>
        <BackLink />
        <ListSkeleton label={t("ledger.detail.loading")} />
      </>
    );
  }
  if (query.isError) {
    if (isApiError(query.error) && query.error.status === 404) {
      return (
        <>
          <BackLink />
          <EmptyState icon={BookOpenText} title={t("ledger.detail.notFoundTitle")} description={t("ledger.detail.notFoundDescription")} />
        </>
      );
    }
    return (
      <>
        <BackLink />
        <QueryErrorState title={t("ledger.detail.loadFailed")} error={query.error} onRetry={() => void query.refetch()} />
      </>
    );
  }
  return <EntryView entry={query.data} />;
}

function EntryView({ entry }: { entry: EntryDetailOut }) {
  const { t } = useI18n();
  const paymentId = paymentIdOf(entry);
  const notSet = <span className="text-ink-muted">{t("ledger.detail.notSet")}</span>;
  const mono = (value: string | null) => (value ? <span className="font-mono text-xs break-all">{value}</span> : notSet);

  return (
    <>
      <BackLink />
      <PageHeader title={t("ledger.detail.title")} />

      <div className="mb-6 flex flex-wrap items-center gap-3">
        <CopyableId id={entry.id} truncate={false} />
        <RecipeLabel recipe={entry.recipe} />
        <EntryStatusBadge status={entry.status} />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <div className="flex min-w-0 flex-col gap-4 xl:col-span-2">
          <Card>
            <CardHeader>
              <CardTitle>{t("ledger.detail.postings")}</CardTitle>
            </CardHeader>
            <CardContent className="px-0 pb-0">
              <PostingsTable postings={entry.postings} />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("ledger.detail.metadata")}</CardTitle>
            </CardHeader>
            <CardContent>
              {isEmptyObject(entry.metadata) ? (
                <p className="text-sm text-ink-muted">{t("ledger.detail.noMetadata")}</p>
              ) : (
                <pre className="overflow-x-auto rounded-sm bg-surface-sunken p-3 font-mono text-xs">{metadataJson(entry.metadata, t("audit.detail.hiddenField"))}</pre>
              )}
            </CardContent>
          </Card>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>{t("ledger.detail.facts")}</CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="flex flex-col gap-3">
              <Fact label={t("ledger.detail.effectiveAt")} value={<DateTime value={entry.effective_at} />} />
              <Fact label={t("ledger.detail.createdAt")} value={<DateTime value={entry.created_at} />} />
              <Fact label={t("ledger.detail.idempotencyKey")} value={mono(entry.idempotency_key)} />
              <Fact label={t("ledger.detail.correlationId")} value={mono(entry.correlation_id)} />
              <Fact label={t("ledger.detail.causationId")} value={mono(entry.causation_id)} />
              {entry.reverses_entry_id ? (
                <Fact label={t("ledger.detail.reverses")} value={<EntryLink entryId={entry.reverses_entry_id} label={entry.reverses_entry_id} />} />
              ) : null}
              {entry.reversed_by_entry_id ? (
                <Fact label={t("ledger.detail.reversedBy")} value={<EntryLink entryId={entry.reversed_by_entry_id} label={entry.reversed_by_entry_id} />} />
              ) : null}
              <Fact
                label={t("ledger.detail.origin")}
                value={
                  paymentId ? (
                    <Link
                      to="/payments/$paymentId"
                      params={{ paymentId }}
                      aria-label={`${t("ledger.detail.openPayment")} ${paymentId}`}
                      className="rounded-xs text-primary underline-offset-2 hover:underline"
                    >
                      {t("ledger.detail.openPayment")}
                    </Link>
                  ) : (
                    notSet
                  )
                }
              />
            </dl>
          </CardContent>
        </Card>
      </div>
    </>
  );
}
