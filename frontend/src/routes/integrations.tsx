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
import { CreateProviderAccountDialog } from "../features/console/provider-dialogs";
import { CredentialsDialog } from "../features/integrations/credentials-dialog";
import { errorText } from "../features/console/errors";
import { ListSkeleton, QueryErrorState } from "../features/console/states";
import { useI18n } from "../i18n";

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
  const { t, tLoose } = useI18n();
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
      title={t("providers.title")}
      description={t("providers.description")}
      scope="sandbox"
      actions={
        <GatedButton
          allowed={mayManage && entries.some((entry) => entry.enabled)}
          reason={mayManage ? t("providers.noneAvailable") : reason}
          onClick={() => setCreating(true)}
        >
          <Plus aria-hidden="true" />
          {t("providers.add")}
        </GatedButton>
      }
    />
  );

  if (!mayRead) {
    return (
      <>
        {header}
        <EmptyState icon={Lock} title={t("providers.unavailableTitle")} description={deniedReason("integrations:read", role)} />
      </>
    );
  }

  function toggle(account: ProviderAccountOut, active: boolean) {
    setActive.mutate(
      { accountId: account.id, active },
      {
        onSuccess: () => {
          toast({ tone: "success", title: active ? t("providers.enabled") : t("providers.disabled"), description: account.name });
          setDisabling(null);
        },
        onError: () => toast({ tone: "danger", title: active ? t("providers.enableFailed") : t("providers.disableFailed") }),
      },
    );
  }

  return (
    <>
      {header}
      {accounts.isPending ? (
        <ListSkeleton label={t("providers.loading")} />
      ) : accounts.isError ? (
        <QueryErrorState title={t("providers.loadFailed")} error={accounts.error} onRetry={() => void accounts.refetch()} />
      ) : accounts.data.data.length === 0 ? (
        <EmptyState
          icon={Plug}
          title={t("providers.emptyTitle")}
          description={t("providers.emptyDescription")}
        />
      ) : (
        <div className="rounded-md border border-line bg-surface">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>{t("providers.cols.label")}</TableHead>
                <TableHead>{t("providers.cols.provider")}</TableHead>
                <TableHead>{t("providers.cols.environment")}</TableHead>
                <TableHead>{t("providers.cols.credentials")}</TableHead>
                <TableHead>{t("providers.cols.status")}</TableHead>
                <TableHead>{t("providers.cols.created")}</TableHead>
                <TableHead>
                  <span className="sr-only">{t("providers.cols.actions")}</span>
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
                      {account.has_credentials ? `•••• ${account.credentials_last4 ?? ""}`.trim() : t("providers.none")}
                    </TableCell>
                    <TableCell>
                      <Badge tone={isActive ? "success" : "neutral"}>{tLoose("status", account.status)}</Badge>
                    </TableCell>
                    <TableCell>
                      <DateTime value={account.created_at} />
                    </TableCell>
                    <TableCell>
                      <div className="flex justify-end gap-1">
                        <GatedButton
                          variant="ghost"
                          size="sm"
                          aria-label={t("providers.updateCredentialsFor", { name: account.name })}
                          allowed={mayManage}
                          reason={reason}
                          onClick={() => setReplacing(account)}
                        >
                          {t("providers.updateCredentials")}
                        </GatedButton>
                        {isActive ? (
                          <GatedButton
                            variant="ghost"
                            size="sm"
                            aria-label={t("providers.disableNamed", { name: account.name })}
                            allowed={mayManage}
                            reason={reason}
                            onClick={() => setDisabling(account)}
                          >
                            {t("providers.disable")}
                          </GatedButton>
                        ) : (
                          <GatedButton
                            variant="ghost"
                            size="sm"
                            aria-label={t("providers.enableNamed", { name: account.name })}
                            allowed={mayManage}
                            reason={reason}
                            onClick={() => toggle(account, true)}
                          >
                            {t("providers.enable")}
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
            <p className="border-t border-line px-3 py-2 text-xs text-ink-muted">{t("providers.firstOnly")}</p>
          ) : null}
        </div>
      )}

      {comingSoon.length > 0 ? (
        <Card className="mt-6">
          <CardHeader>
            <CardTitle>{t("providers.others")}</CardTitle>
            <CardDescription>{t("providers.othersDescription")}</CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="flex flex-wrap gap-2">
              {comingSoon.map((entry) => (
                <li key={entry.provider}>
                  <Badge tone="neutral">{t("providers.comingSoon", { provider: entry.provider })}</Badge>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}

      <CreateProviderAccountDialog projectId={projectId} catalog={entries} open={creating} onOpenChange={setCreating} />
      <CredentialsDialog
        projectId={projectId}
        account={replacing}
        entry={entries.find((entry) => entry.provider === replacing?.provider)}
        onClose={() => setReplacing(null)}
      />
      <ConfirmDialog
        open={disabling !== null}
        onOpenChange={(open) => (open ? undefined : setDisabling(null))}
        title={t("providers.disableTitle")}
        description={t("providers.disableDescription", { name: disabling?.name ?? t("providers.thisAccount") })}
        confirmLabel={t("providers.disableLabel")}
        tone="danger"
        pending={setActive.isPending}
        error={setActive.isError ? errorText(setActive.error) : null}
        onConfirm={() => disabling && toggle(disabling, false)}
      />
    </>
  );
}
