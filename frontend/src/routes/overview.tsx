import { Link } from "@tanstack/react-router";

import { PageHeader } from "../components/page-header";
import { Badge } from "../components/ui/badge";
import { buttonVariants } from "../components/ui/button-variants";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Skeleton } from "../components/ui/skeleton";
import { useApiKeys, useProviderAccounts, useTrialBalance } from "../features/console/api";
import { NoProjectState } from "../features/console/no-project";
import { useProject } from "../features/console/use-project";
import { errorText } from "../features/console/errors";
import { HealthIndicator } from "../features/health/health-indicator";
import { StreamStatusIndicator } from "../lib/realtime/stream-status";

// Widened to string: the route tree is assembled at runtime, so its paths are not literal types.
const LINKS: Record<"apikeys" | "integrations" | "projects" | "members", string> = {
  apikeys: "/api-keys",
  integrations: "/integrations",
  projects: "/projects",
  members: "/members",
};

function Count({ value, hasMore }: { value: number; hasMore: boolean }) {
  return (
    <p className="figures text-3xl font-semibold">
      {value}
      {hasMore ? "+" : ""}
    </p>
  );
}

function CardBody({ pending, error, children }: { pending: boolean; error: unknown; children: React.ReactNode }) {
  if (pending) return <Skeleton className="h-8 w-24" aria-label="Loading" role="status" />;
  if (error) return <p className="text-sm text-danger">{errorText(error)}</p>;
  return <>{children}</>;
}

export function OverviewPage() {
  const { membership, can, stream } = useProject();
  const projectId = membership?.project_id ?? null;
  const mayKeys = can("api_keys:manage");
  const mayAccounts = can("integrations:read");
  const mayLedger = can("ledger:read");
  const balance = useTrialBalance(mayLedger ? projectId : null);
  const keys = useApiKeys(projectId, mayKeys);
  const accounts = useProviderAccounts(projectId, mayAccounts);

  if (!membership) {
    return (
      <>
        <PageHeader title="Overview" description="The state of your active project." scope="implemented" />
        <NoProjectState />
      </>
    );
  }

  const activeKeys = keys.data?.data.filter((key) => key.status === "active").length ?? 0;
  const activeAccounts = accounts.data?.data.filter((account) => account.status === "active").length ?? 0;

  return (
    <>
      <PageHeader title="Overview" description="The state of your active project." scope="implemented" />
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle>Ledger</CardTitle>
            <CardDescription>Trial balance</CardDescription>
          </CardHeader>
          <CardContent>
            {!mayLedger ? (
              <p className="text-sm text-ink-muted">Not available for your role.</p>
            ) : (
              <CardBody pending={balance.isPending} error={balance.error}>
                {balance.data && balance.data.totals.length === 0 ? (
                  <p className="text-sm text-ink-muted">No entries yet.</p>
                ) : (
                  <ul className="flex flex-col gap-1">
                    {balance.data?.totals.map((total) => (
                      <li key={total.currency} className="flex items-center gap-2 text-sm">
                        <span className="font-mono">{total.currency}</span>
                        <Badge tone={total.balanced ? "success" : "danger"}>
                          {total.balanced ? "Balanced" : "Not balanced"}
                        </Badge>
                      </li>
                    ))}
                  </ul>
                )}
              </CardBody>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Active API keys</CardTitle>
            <CardDescription>
              <Link to={LINKS.apikeys} className={buttonVariants({ variant: "link", size: "sm" })}>
                Manage API keys
              </Link>
            </CardDescription>
          </CardHeader>
          <CardContent>
            {!mayKeys ? (
              <p className="text-sm text-ink-muted">Not available for your role.</p>
            ) : (
              <CardBody pending={keys.isPending} error={keys.error}>
                <Count value={activeKeys} hasMore={keys.data?.has_more ?? false} />
              </CardBody>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Active provider accounts</CardTitle>
            <CardDescription>
              <Link to={LINKS.integrations} className={buttonVariants({ variant: "link", size: "sm" })}>
                Manage provider accounts
              </Link>
            </CardDescription>
          </CardHeader>
          <CardContent>
            {!mayAccounts ? (
              <p className="text-sm text-ink-muted">Not available for your role.</p>
            ) : (
              <CardBody pending={accounts.isPending} error={accounts.error}>
                <Count value={activeAccounts} hasMore={accounts.data?.has_more ?? false} />
              </CardBody>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Realtime</CardTitle>
            <CardDescription>Live updates for this project.</CardDescription>
          </CardHeader>
          <CardContent>
            <StreamStatusIndicator status={stream.status} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Environment</CardTitle>
            <CardDescription>Live check against the API.</CardDescription>
          </CardHeader>
          <CardContent>
            <HealthIndicator />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Quick links</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="flex flex-col gap-1 text-sm">
              <li>
                <Link to={LINKS.projects} className="underline">
                  Projects
                </Link>
              </li>
              <li>
                <Link to={LINKS.members} className="underline">
                  Members
                </Link>
              </li>
              <li>
                <Link to={LINKS.integrations} className="underline">
                  Provider accounts
                </Link>
              </li>
            </ul>
          </CardContent>
        </Card>
      </div>
    </>
  );
}
