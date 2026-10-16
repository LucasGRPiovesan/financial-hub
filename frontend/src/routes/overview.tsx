import { Link } from "@tanstack/react-router";
import { Scale } from "lucide-react";
import type { ReactNode } from "react";

import type { AuditRecordOut, TrialBalanceTotalOut } from "../api/generated/model";
import { RelativeTime } from "../components/date-time";
import { Money } from "../components/money";
import { PageHeader } from "../components/page-header";
import { Badge } from "../components/ui/badge";
import { buttonVariants } from "../components/ui/button-variants";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Skeleton } from "../components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { useApiKeys, useProviderAccounts, useTrialBalance } from "../features/console/api";
import { useSession } from "../features/auth/use-session";
import { describeActivity, type OperatorNames } from "../features/console/audit-activity";
import { useRecentAudit } from "../features/console/audit-api";
import { flattenCustomers, useCustomers } from "../features/console/customers-api";
import { errorText } from "../features/console/errors";
import { useMembers } from "../features/console/members-api";
import { NoProjectState } from "../features/console/no-project";
import { useProject } from "../features/console/use-project";
import { HealthIndicator } from "../features/health/health-indicator";
import { useT } from "../i18n";
import { StreamStatusIndicator } from "../lib/realtime/stream-status";

// Widened to string: the route tree is assembled at runtime, so its paths are not literal types.
const LINKS: Record<"apikeys" | "integrations" | "members" | "customers", string> = {
  apikeys: "/api-keys",
  integrations: "/integrations",
  members: "/members",
  customers: "/customers",
};

function Stat({
  title,
  href,
  linkLabel,
  allowed,
  pending,
  error,
  value,
  hasMore,
}: {
  title: string;
  href: string;
  linkLabel: string;
  allowed: boolean;
  pending: boolean;
  error: unknown;
  value: number;
  hasMore: boolean;
}) {
  const t = useT();
  let body: ReactNode;
  if (!allowed) body = <p className="text-sm text-ink-muted">{t("common.unavailableForRole")}</p>;
  else if (pending) body = <Skeleton className="h-9 w-16" aria-label={t("overview.loadingStat", { title: title.toLowerCase() })} role="status" />;
  else if (error) body = <p className="text-sm text-danger">{errorText(error)}</p>;
  else {
    body = (
      <p className="figures text-3xl font-semibold tracking-tight">
        {value}
        {hasMore ? "+" : ""}
      </p>
    );
  }
  return (
    <Card>
      <CardContent className="flex h-full flex-col justify-between gap-3">
        <h2 className="text-sm font-medium text-ink-muted">{title}</h2>
        <div className="min-h-9">{body}</div>
        <Link to={href} className={buttonVariants({ variant: "link", size: "sm" }) + " self-start px-0!"}>
          {linkLabel}
        </Link>
      </CardContent>
    </Card>
  );
}

/** Totals arrive as JSON numbers of minor units; anything beyond the safe range is left out rather than rounded. */
function minorString(value: number): string | null {
  return Number.isSafeInteger(value) ? value.toString() : null;
}

function TotalAmount({ currency, value }: { currency: string; value: number }) {
  const minor = minorString(value);
  if (minor === null) return <span className="text-ink-faint">-</span>;
  return <Money value={{ amount_minor: minor, currency }} />;
}

function TrialBalanceTable({ totals }: { totals: TrialBalanceTotalOut[] }) {
  const t = useT();
  return (
    <Table>
      <caption className="sr-only">{t("overview.trial.caption")}</caption>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead>{t("overview.trial.currency")}</TableHead>
          <TableHead className="text-right">{t("overview.trial.debits")}</TableHead>
          <TableHead className="text-right">{t("overview.trial.credits")}</TableHead>
          <TableHead>{t("overview.trial.status")}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {totals.map((total) => (
          <TableRow key={total.currency}>
            <TableCell className="font-mono">{total.currency}</TableCell>
            <TableCell className="text-right">
              <TotalAmount currency={total.currency} value={total.debits} />
            </TableCell>
            <TableCell className="text-right">
              <TotalAmount currency={total.currency} value={total.credits} />
            </TableCell>
            <TableCell>
              <Badge tone={total.balanced ? "success" : "danger"}>{total.balanced ? t("overview.trial.balanced") : t("overview.trial.unbalanced")}</Badge>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

const ACTIVITY_ROWS = 6;

function ActivityItem({ record, names }: { record: AuditRecordOut; names: OperatorNames }) {
  const t = useT();
  const line = describeActivity(record, names);
  const ids = line.ids.join(" · ");
  return (
    <li className="flex items-start justify-between gap-4 border-b border-line py-2.5 last:border-b-0">
      <div className="min-w-0">
        <p className="truncate text-sm">{line.sentence}</p>
        <p className="truncate text-xs text-ink-muted">
          {t("activity.by", { actor: line.actor })}
          {ids ? (
            <>
              {" "}
              <span className="font-mono text-ink-faint" title={ids}>
                {ids}
              </span>
            </>
          ) : null}
        </p>
      </div>
      <RelativeTime value={record.occurred_at} className="shrink-0 text-xs text-ink-muted" />
    </li>
  );
}

export function OverviewPage() {
  const t = useT();
  const { membership, can, stream } = useProject();
  const projectId = membership?.project_id ?? null;
  const mayKeys = can("api_keys:manage");
  const mayAccounts = can("integrations:read");
  const mayLedger = can("ledger:read");
  const mayMembers = can("members:read");
  const mayCustomers = can("customers:read");
  const mayAudit = can("audit:read");
  const balance = useTrialBalance(mayLedger ? projectId : null);
  const keys = useApiKeys(projectId, mayKeys);
  const accounts = useProviderAccounts(projectId, mayAccounts);
  const members = useMembers(mayMembers ? projectId : null);
  const customers = useCustomers(projectId, null, mayCustomers);
  const audit = useRecentAudit(projectId, mayAudit);
  const { operator } = useSession();
  const names: OperatorNames = new Map([
    [operator.id, operator.display_name],
    ...(members.data?.data ?? []).map((member): [string, string] => [member.operator_id, member.display_name]),
  ]);

  if (!membership) {
    return (
      <>
        <PageHeader title={t("overview.title")} description={t("overview.description")} />
        <NoProjectState />
      </>
    );
  }

  const activeKeys = keys.data?.data.filter((key) => key.status === "active").length ?? 0;
  const activeAccounts = accounts.data?.data.filter((account) => account.status === "active").length ?? 0;
  const customerCount = flattenCustomers(customers.data).length;

  let ledger: ReactNode;
  if (!mayLedger) ledger = <p className="px-5 py-4 text-sm text-ink-muted">{t("common.unavailableForRole")}</p>;
  else if (balance.isPending) {
    ledger = (
      <div className="px-5 py-4">
        <Skeleton className="h-16 w-full" aria-label={t("overview.trial.loading")} role="status" />
      </div>
    );
  } else if (balance.error) ledger = <p className="px-5 py-4 text-sm text-danger">{errorText(balance.error)}</p>;
  else if (balance.data.totals.length === 0) {
    ledger = (
      <div className="flex items-center gap-4 px-5 py-6">
        <span aria-hidden="true" className="flex size-10 shrink-0 items-center justify-center rounded-full bg-surface-sunken text-ink-muted">
          <Scale className="size-5" />
        </span>
        <div className="min-w-0">
          <p className="text-sm font-medium">{t("overview.trial.empty")}</p>
          <p className="text-sm text-ink-muted">{t("overview.trial.emptyHint")}</p>
        </div>
      </div>
    );
  }
  else ledger = <TrialBalanceTable totals={balance.data.totals} />;

  let activity: ReactNode;
  if (!mayAudit) activity = <p className="px-5 py-4 text-sm text-ink-muted">{t("common.unavailableForRole")}</p>;
  else if (audit.isPending) {
    activity = (
      <div className="px-5 py-4">
        <Skeleton className="h-24 w-full" aria-label={t("overview.activity.loading")} role="status" />
      </div>
    );
  } else if (audit.error) activity = <p className="px-5 py-4 text-sm text-danger">{errorText(audit.error)}</p>;
  else if (audit.data.data.length === 0) activity = <p className="px-5 py-4 text-sm text-ink-muted">{t("overview.activity.none")}</p>;
  else {
    activity = (
      <ul aria-label={t("overview.activity.title")} className="px-5">
        {audit.data.data.slice(0, ACTIVITY_ROWS).map((record) => (
          <ActivityItem key={record.id} record={record} names={names} />
        ))}
      </ul>
    );
  }

  return (
    <>
      <PageHeader title={t("overview.title")} description={t("overview.description")} />

      <section aria-label={t("overview.keyFigures")} className="grid grid-cols-[minmax(0,1fr)] gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          title={t("overview.stats.apiKeys.title")}
          href={LINKS.apikeys}
          linkLabel={t("overview.stats.apiKeys.link")}
          allowed={mayKeys}
          pending={keys.isPending}
          error={keys.error}
          value={activeKeys}
          hasMore={keys.data?.has_more ?? false}
        />
        <Stat
          title={t("overview.stats.accounts.title")}
          href={LINKS.integrations}
          linkLabel={t("overview.stats.accounts.link")}
          allowed={mayAccounts}
          pending={accounts.isPending}
          error={accounts.error}
          value={activeAccounts}
          hasMore={accounts.data?.has_more ?? false}
        />
        <Stat
          title={t("overview.stats.members.title")}
          href={LINKS.members}
          linkLabel={t("overview.stats.members.link")}
          allowed={mayMembers}
          pending={members.isPending}
          error={members.error}
          value={members.data?.data.length ?? 0}
          hasMore={false}
        />
        <Stat
          title={t("overview.stats.customers.title")}
          href={LINKS.customers}
          linkLabel={t("overview.stats.customers.link")}
          allowed={mayCustomers}
          pending={customers.isPending}
          error={customers.error}
          value={customerCount}
          hasMore={customers.hasNextPage}
        />
      </section>

      <div className="mt-4 grid grid-cols-[minmax(0,1fr)] items-start gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Card className="min-w-0 overflow-hidden">
          <CardHeader>
            <CardTitle>{t("overview.trial.title")}</CardTitle>
            <CardDescription>{t("overview.trial.description")}</CardDescription>
          </CardHeader>
          {ledger}
        </Card>

        <Card className="min-w-0 overflow-hidden">
          <CardHeader>
            <CardTitle>{t("overview.activity.title")}</CardTitle>
            <CardDescription>{t("overview.activity.description")}</CardDescription>
          </CardHeader>
          {activity}
        </Card>
      </div>

      <section aria-label={t("overview.system")} className="mt-4 grid grid-cols-[minmax(0,1fr)] gap-4 sm:grid-cols-2">
        <Card>
          <CardContent className="flex flex-col gap-2">
            <h2 className="text-sm font-medium text-ink-muted">{t("overview.realtime")}</h2>
            <StreamStatusIndicator status={stream.status} />
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex flex-col gap-2">
            <h2 className="text-sm font-medium text-ink-muted">{t("overview.environment")}</h2>
            <HealthIndicator />
          </CardContent>
        </Card>
      </section>
    </>
  );
}
