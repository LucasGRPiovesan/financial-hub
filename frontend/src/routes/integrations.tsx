import { Lock, Plug, Plus } from "lucide-react";
import { useState } from "react";

import type { ProviderAccountOut } from "../api/generated/model";
import { ConfirmDialog } from "../components/confirm-dialog";
import { DateTime } from "../components/date-time";
import { PageHeader } from "../components/page-header";
import { Badge } from "../components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { EmptyState } from "../components/ui/empty-state";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { useToast } from "../components/ui/use-toast";
import { useProviderAccounts, useProviderCatalog, useSetProviderAccountActive } from "../features/console/api";
import { GatedButton } from "../features/console/gated-button";
import { NoProjectState } from "../features/console/no-project";
import { deniedReason } from "../features/console/permissions";
import { useProject } from "../features/console/use-project";
import { CreateProviderAccountDialog, ReplaceCredentialsDialog } from "../features/console/provider-dialogs";
import { errorText } from "../features/console/errors";
import { ListSkeleton, QueryErrorState } from "../features/console/states";

export function IntegrationsPage() {
  const { membership, can } = useProject();
  if (!membership) return <NoProjectState />;
  return (
    <ProviderAccountsForProject
      projectId={membership.project_id}
      role={membership.role}
      mayRead={can("integrations:read")}
      mayManage={can("integrations:manage")}
    />
  );
}

function ProviderAccountsForProject({
  projectId,
  role,
  mayRead,
  mayManage,
}: {
  projectId: string;
  role: string;
  mayRead: boolean;
  mayManage: boolean;
}) {
  const toast = useToast();
  const accounts = useProviderAccounts(projectId, mayRead);
  const catalog = useProviderCatalog();
  const setActive = useSetProviderAccountActive(projectId);
  const [creating, setCreating] = useState(false);
  const [replacing, setReplacing] = useState<ProviderAccountOut | null>(null);
  const [disabling, setDisabling] = useState<ProviderAccountOut | null>(null);

  const entries = catalog.data ?? [];
  const comingSoon = entries.filter((entry) => !entry.enabled);
  const reason = deniedReason("integrations:manage", role);

  const header = (
    <PageHeader
      title="Provider accounts"
      description="Connect this project to payment providers. Asaas Sandbox is the only live integration."
      scope="sandbox"
      actions={
        <GatedButton
          allowed={mayManage && entries.some((entry) => entry.enabled)}
          reason={mayManage ? "No provider is available yet." : reason}
          onClick={() => setCreating(true)}
        >
          <Plus aria-hidden="true" />
          Add provider account
        </GatedButton>
      }
    />
  );

  if (!mayRead) {
    return (
      <>
        {header}
        <EmptyState icon={Lock} title="Provider accounts aren't available for your role" description={deniedReason("integrations:read", role)} />
      </>
    );
  }

  function toggle(account: ProviderAccountOut, active: boolean) {
    setActive.mutate(
      { accountId: account.id, active },
      {
        onSuccess: () => {
          toast({ tone: "success", title: active ? "Provider account enabled" : "Provider account disabled", description: account.name });
          setDisabling(null);
        },
        onError: () => toast({ tone: "danger", title: active ? "Couldn't enable the account" : "Couldn't disable the account" }),
      },
    );
  }

  return (
    <>
      {header}
      {accounts.isPending ? (
        <ListSkeleton label="Loading provider accounts" />
      ) : accounts.isError ? (
        <QueryErrorState title="Provider accounts couldn't load" error={accounts.error} onRetry={() => void accounts.refetch()} />
      ) : accounts.data.data.length === 0 ? (
        <EmptyState
          icon={Plug}
          title="No provider accounts yet"
          description="Add an Asaas Sandbox account to start testing payments."
        />
      ) : (
        <div className="rounded-md border border-line bg-surface">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Label</TableHead>
                <TableHead>Provider</TableHead>
                <TableHead>Environment</TableHead>
                <TableHead>Credentials</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {accounts.data.data.map((account) => {
                const isActive = account.status === "active";
                return (
                  <TableRow key={account.id}>
                    <TableCell className="font-medium">{account.name}</TableCell>
                    <TableCell>{account.provider}</TableCell>
                    <TableCell>{account.environment}</TableCell>
                    <TableCell className="font-mono text-xs text-ink-muted">
                      {account.has_credentials ? `•••• ${account.credentials_last4 ?? ""}`.trim() : "None"}
                    </TableCell>
                    <TableCell>
                      <Badge tone={isActive ? "success" : "neutral"}>{account.status}</Badge>
                    </TableCell>
                    <TableCell>
                      <DateTime value={account.created_at} />
                    </TableCell>
                    <TableCell>
                      <div className="flex justify-end gap-1">
                        <GatedButton
                          variant="ghost"
                          size="sm"
                          aria-label={`Update credentials for ${account.name}`}
                          allowed={mayManage}
                          reason={reason}
                          onClick={() => setReplacing(account)}
                        >
                          Update credentials
                        </GatedButton>
                        {isActive ? (
                          <GatedButton
                            variant="ghost"
                            size="sm"
                            aria-label={`Disable ${account.name}`}
                            allowed={mayManage}
                            reason={reason}
                            onClick={() => setDisabling(account)}
                          >
                            Disable
                          </GatedButton>
                        ) : (
                          <GatedButton
                            variant="ghost"
                            size="sm"
                            aria-label={`Enable ${account.name}`}
                            allowed={mayManage}
                            reason={reason}
                            onClick={() => toggle(account, true)}
                          >
                            Enable
                          </GatedButton>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
          {accounts.data.has_more ? (
            <p className="border-t border-line px-3 py-2 text-xs text-ink-muted">Showing the first accounts only.</p>
          ) : null}
        </div>
      )}

      {comingSoon.length > 0 ? (
        <Card className="mt-6">
          <CardHeader>
            <CardTitle>Other providers</CardTitle>
            <CardDescription>Not available yet.</CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="flex flex-wrap gap-2">
              {comingSoon.map((entry) => (
                <li key={entry.provider}>
                  <Badge tone="neutral">{entry.provider} — coming soon</Badge>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}

      <CreateProviderAccountDialog projectId={projectId} catalog={entries} open={creating} onOpenChange={setCreating} />
      <ReplaceCredentialsDialog
        projectId={projectId}
        account={replacing}
        entry={entries.find((entry) => entry.provider === replacing?.provider)}
        onClose={() => setReplacing(null)}
      />
      <ConfirmDialog
        open={disabling !== null}
        onOpenChange={(open) => (open ? undefined : setDisabling(null))}
        title="Disable provider account?"
        description={`${disabling?.name ?? "This account"} will stop processing requests until you enable it again.`}
        confirmLabel="Disable account"
        tone="danger"
        pending={setActive.isPending}
        error={setActive.isError ? errorText(setActive.error) : null}
        onConfirm={() => disabling && toggle(disabling, false)}
      />
    </>
  );
}
