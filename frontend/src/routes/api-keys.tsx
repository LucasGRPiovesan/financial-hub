import { KeyRound, Lock, Plus } from "lucide-react";
import { useState } from "react";

import type { ApiKeyOut } from "../api/generated/model";
import { ConfirmDialog } from "../components/confirm-dialog";
import { DateTime } from "../components/date-time";
import { PageHeader } from "../components/page-header";
import { Badge } from "../components/ui/badge";
import { EmptyState } from "../components/ui/empty-state";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { useToast } from "../components/ui/use-toast";
import { CreateApiKeyDialog } from "../features/console/api-key-dialogs";
import { useApiKeys, useRevokeApiKey } from "../features/console/api";
import { GatedButton } from "../features/console/gated-button";
import { useI18n } from "../i18n";
import { NoProjectState } from "../features/console/no-project";
import { deniedReason } from "../features/console/permissions";
import { useProject } from "../features/console/use-project";
import { errorText } from "../features/console/errors";
import { ListSkeleton, QueryErrorState } from "../features/console/states";

const STATUS_TONE = { active: "success", revoked: "danger", expired: "warning" } as const;

export function ApiKeysPage() {
  const { membership, can } = useProject();
  if (!membership) return <NoProjectState />;
  return <ApiKeysForProject projectId={membership.project_id} role={membership.role} mayManage={can("api_keys:manage")} />;
}

function ApiKeysForProject({ projectId, role, mayManage }: { projectId: string; role: string; mayManage: boolean }) {
  const toast = useToast();
  const { t, tLoose } = useI18n();
  const keys = useApiKeys(projectId, mayManage);
  const revoke = useRevokeApiKey(projectId);
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<ApiKeyOut | null>(null);

  const header = (
    <PageHeader
      title={t("apiKeys.title")}
      description={t("apiKeys.description")}
      actions={
        <GatedButton allowed={mayManage} reason={deniedReason("api_keys:manage", role)} onClick={() => setCreating(true)}>
          <Plus aria-hidden="true" />
          {t("apiKeys.create")}
        </GatedButton>
      }
    />
  );

  if (!mayManage) {
    return (
      <>
        {header}
        <EmptyState
          icon={Lock}
          title={t("apiKeys.unavailableTitle")}
          description={deniedReason("api_keys:manage", role)}
        />
      </>
    );
  }

  function confirmRevoke() {
    if (!revoking) return;
    const target = revoking;
    revoke.mutate(target.id, {
      onSuccess: () => {
        toast({ tone: "success", title: t("apiKeys.revoked"), description: target.name });
        setRevoking(null);
      },
      onError: () => toast({ tone: "danger", title: t("apiKeys.revokeFailed") }),
    });
  }

  return (
    <>
      {header}
      {keys.isPending ? (
        <ListSkeleton label={t("apiKeys.loading")} />
      ) : keys.isError ? (
        <QueryErrorState title={t("apiKeys.loadFailed")} error={keys.error} onRetry={() => void keys.refetch()} />
      ) : keys.data.data.length === 0 ? (
        <EmptyState
          icon={KeyRound}
          title={t("apiKeys.emptyTitle")}
          description={t("apiKeys.emptyDescription")}
        />
      ) : (
        <div className="rounded-md border border-line bg-surface">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>{t("apiKeys.cols.name")}</TableHead>
                <TableHead>{t("apiKeys.cols.prefix")}</TableHead>
                <TableHead>{t("apiKeys.cols.scopes")}</TableHead>
                <TableHead>{t("apiKeys.cols.created")}</TableHead>
                <TableHead>{t("apiKeys.cols.lastUsed")}</TableHead>
                <TableHead>{t("apiKeys.cols.expires")}</TableHead>
                <TableHead>{t("apiKeys.cols.status")}</TableHead>
                <TableHead>
                  <span className="sr-only">{t("apiKeys.cols.actions")}</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {keys.data.data.map((key) => {
                const tone = STATUS_TONE[key.status as keyof typeof STATUS_TONE] ?? "neutral";
                return (
                  <TableRow key={key.id}>
                    <TableCell className="font-medium">{key.name}</TableCell>
                    <TableCell className="font-mono text-xs text-ink-muted">{key.prefix}</TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {key.scopes.map((scope) => (
                          <Badge key={scope} tone="neutral" className="font-mono">
                            {scope}
                          </Badge>
                        ))}
                      </div>
                    </TableCell>
                    <TableCell>
                      <DateTime value={key.created_at} />
                    </TableCell>
                    <TableCell>{key.last_used_at ? <DateTime value={key.last_used_at} /> : t("apiKeys.never")}</TableCell>
                    <TableCell>{key.expires_at ? <DateTime value={key.expires_at} /> : t("apiKeys.never")}</TableCell>
                    <TableCell>
                      <Badge tone={tone}>{tLoose("status", key.status)}</Badge>
                    </TableCell>
                    <TableCell>
                      <div className="flex justify-end">
                        {key.status === "revoked" ? null : (
                          <GatedButton
                            variant="ghost"
                            size="sm"
                            aria-label={t("apiKeys.revokeNamed", { name: key.name })}
                            allowed={mayManage}
                            reason={deniedReason("api_keys:manage", role)}
                            onClick={() => setRevoking(key)}
                          >
                            {t("apiKeys.revoke")}
                          </GatedButton>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
          {keys.data.has_more ? (
            <p className="border-t border-line px-3 py-2 text-xs text-ink-muted">{t("apiKeys.firstOnly")}</p>
          ) : null}
        </div>
      )}

      <CreateApiKeyDialog projectId={projectId} open={creating} onOpenChange={setCreating} />
      <ConfirmDialog
        open={revoking !== null}
        onOpenChange={(open) => (open ? undefined : setRevoking(null))}
        title={t("apiKeys.confirmTitle")}
        description={t("apiKeys.confirmDescription", { name: revoking?.name ?? t("apiKeys.confirmThisKey") })}
        confirmLabel={t("apiKeys.confirmLabel")}
        tone="danger"
        pending={revoke.isPending}
        error={revoke.isError ? errorText(revoke.error) : null}
        onConfirm={confirmRevoke}
      />
    </>
  );
}
