import { Link } from "@tanstack/react-router";
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
import { StreamStatusIndicator } from "../lib/realtime/stream-status";

// Widened to string: the route tree is assembled at runtime, so its paths are not literal types.
const LINKS: Record<"apikeys" | "integrations" | "members" | "customers", string> = {
  apikeys: "/api-keys",
  integrations: "/integrations",
  members: "/members",
  customers: "/customers",
};

const DESCRIPTION = "The state of your active project.";

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
  let body: ReactNode;
  if (!allowed) body = <p className="text-sm text-ink-muted">Not available for your role.</p>;
  else if (pending) body = <Skeleton className="h-9 w-16" aria-label={`Loading ${title.toLowerCase()}`} role="status" />;
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
        <Link to={href} className={buttonVariants({ variant: "link", size: "sm" }) + " self-start px-0"}>
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
  return (
    <Table>
      <caption className="sr-only">Trial balance by currency</caption>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead>Currency</TableHead>
          <TableHead className="text-right">Debits</TableHead>
          <TableHead className="text-right">Credits</TableHead>
          <TableHead>Status</TableHead>
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
              <Badge tone={total.balanced ? "success" : "danger"}>{total.balanced ? "Balanced" : "Not balanced"}</Badge>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

const ACTIVITY_ROWS = 6;

function ActivityItem({ record, names }: { record: AuditRecordOut; names: OperatorNames }) {
  const line = describeActivity(record, names);
  const ids = line.ids.join(" · ");
  return (
    <li className="flex items-start justify-between gap-4 border-b border-line py-2.5 last:border-b-0">
      <div className="min-w-0">
        <p className="truncate text-sm">{line.sentence}</p>
        <p className="truncate text-xs text-ink-muted">
          by {line.actor}
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
        <PageHeader title="Overview" description={DESCRIPTION} />
        <NoProjectState />
      </>
    );
  }

  const activeKeys = keys.data?.data.filter((key) => key.status === "active").length ?? 0;
  const activeAccounts = accounts.data?.data.filter((account) => account.status === "active").length ?? 0;
  const customerCount = flattenCustomers(customers.data).length;

  let ledger: ReactNode;
  if (!mayLedger) ledger = <p className="px-5 py-4 text-sm text-ink-muted">Not available for your role.</p>;
  else if (balance.isPending) {
    ledger = (
      <div className="px-5 py-4">
        <Skeleton className="h-16 w-full" aria-label="Loading trial balance" role="status" />
      </div>
    );
  } else if (balance.error) ledger = <p className="px-5 py-4 text-sm text-danger">{errorText(balance.error)}</p>;
  else if (balance.data.totals.length === 0) ledger = <p className="px-5 py-4 text-sm text-ink-muted">
      No entries yet. Posted ledger entries appear here as debit and credit totals per currency.
    </p>;
  else ledger = <TrialBalanceTable totals={balance.data.totals} />;

  let activity: ReactNode;
  if (!mayAudit) activity = <p className="px-5 py-4 text-sm text-ink-muted">Not available for your role.</p>;
  else if (audit.isPending) {
    activity = (
      <div className="px-5 py-4">
        <Skeleton className="h-24 w-full" aria-label="Loading recent activity" role="status" />
      </div>
    );
  } else if (audit.error) activity = <p className="px-5 py-4 text-sm text-danger">{errorText(audit.error)}</p>;
  else if (audit.data.data.length === 0) activity = <p className="px-5 py-4 text-sm text-ink-muted">No activity recorded yet.</p>;
  else {
    activity = (
      <ul aria-label="Recent activity" className="px-5">
        {audit.data.data.slice(0, ACTIVITY_ROWS).map((record) => (
          <ActivityItem key={record.id} record={record} names={names} />
        ))}
      </ul>
    );
  }

  return (
    <>
      <PageHeader title="Overview" description={DESCRIPTION} />

      <section aria-label="Key figures" className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          title="Active API keys"
          href={LINKS.apikeys}
          linkLabel="Manage API keys"
          allowed={mayKeys}
          pending={keys.isPending}
          error={keys.error}
          value={activeKeys}
          hasMore={keys.data?.has_more ?? false}
        />
        <Stat
          title="Active provider accounts"
          href={LINKS.integrations}
          linkLabel="Manage provider accounts"
          allowed={mayAccounts}
          pending={accounts.isPending}
          error={accounts.error}
          value={activeAccounts}
          hasMore={accounts.data?.has_more ?? false}
        />
        <Stat
          title="Members"
          href={LINKS.members}
          linkLabel="See members"
          allowed={mayMembers}
          pending={members.isPending}
          error={members.error}
          value={members.data?.data.length ?? 0}
          hasMore={false}
        />
        <Stat
          title="Customers"
          href={LINKS.customers}
          linkLabel="Browse customers"
          allowed={mayCustomers}
          pending={customers.isPending}
          error={customers.error}
          value={customerCount}
          hasMore={customers.hasNextPage}
        />
      </section>

      <div className="mt-4 grid items-start gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Card className="overflow-hidden">
          <CardHeader>
            <CardTitle>Trial balance</CardTitle>
            <CardDescription>Debits and credits per currency. They must match.</CardDescription>
          </CardHeader>
          {ledger}
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Recent activity</CardTitle>
            <CardDescription>The latest audited actions in this project.</CardDescription>
          </CardHeader>
          {activity}
        </Card>
      </div>

      <section aria-label="System status" className="mt-4 grid gap-4 sm:grid-cols-2">
        <Card>
          <CardContent className="flex flex-col gap-2">
            <h2 className="text-sm font-medium text-ink-muted">Realtime</h2>
            <StreamStatusIndicator status={stream.status} />
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex flex-col gap-2">
            <h2 className="text-sm font-medium text-ink-muted">Environment</h2>
            <HealthIndicator />
          </CardContent>
        </Card>
      </section>
    </>
  );
}
