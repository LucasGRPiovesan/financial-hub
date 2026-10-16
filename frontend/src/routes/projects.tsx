import { useSuspenseQuery } from "@tanstack/react-query";
import { FolderKanban, Plus, Settings2 } from "lucide-react";
import { useState } from "react";

import type { ProjectOut } from "../api/generated/model";
import { DateTime } from "../components/date-time";
import { PageHeader } from "../components/page-header";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { EmptyState } from "../components/ui/empty-state";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { meQueryOptions } from "../features/auth/auth-api";
import { useProjects } from "../features/console/api";
import { GatedButton } from "../features/console/gated-button";
import { can, deniedReason } from "../features/console/permissions";
import { CreateProjectDialog, RenameProjectDialog } from "../features/console/project-dialogs";
import { useProject } from "../features/console/use-project";
import { ListSkeleton, QueryErrorState } from "../features/console/states";
import { useI18n } from "../i18n";
import { selectProject } from "../lib/project-selection";

export function ProjectsPage() {
  const { t, tLoose } = useI18n();
  const { data: me } = useSuspenseQuery(meQueryOptions);
  const { membership } = useProject();
  const projects = useProjects();
  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<ProjectOut | null>(null);

  const roleOf = (projectId: string) => me.memberships.find((item) => item.project_id === projectId)?.role;

  return (
    <>
      <PageHeader
        title={t("projects.title")}
        description={t("projects.description")}
        actions={
          <Button onClick={() => setCreating(true)}>
            <Plus aria-hidden="true" />
            {t("projects.create")}
          </Button>
        }
      />

      {projects.isPending ? (
        <ListSkeleton label={t("projects.loading")} />
      ) : projects.isError ? (
        <QueryErrorState title={t("projects.loadFailed")} error={projects.error} onRetry={() => void projects.refetch()} />
      ) : projects.data.data.length === 0 ? (
        <EmptyState
          icon={FolderKanban}
          title={t("projects.emptyTitle")}
          description={t("projects.emptyDescription")}
          action={<Button onClick={() => setCreating(true)}>{t("projects.create")}</Button>}
        />
      ) : (
        <div className="rounded-md border border-line bg-surface">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>{t("projects.cols.name")}</TableHead>
                <TableHead>{t("projects.cols.slug")}</TableHead>
                <TableHead>{t("projects.cols.role")}</TableHead>
                <TableHead>{t("projects.cols.status")}</TableHead>
                <TableHead>{t("projects.cols.created")}</TableHead>
                <TableHead>
                  <span className="sr-only">{t("projects.cols.actions")}</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {projects.data.data.map((project) => {
                const role = roleOf(project.id);
                const mayManage = can(role, "projects:manage");
                const active = project.id === membership?.project_id;
                return (
                  <TableRow key={project.id}>
                    <TableCell className="font-medium">
                      {project.name}
                      {active ? (
                        <Badge tone="primary" className="ml-2">
                          {t("projects.active")}
                        </Badge>
                      ) : null}
                    </TableCell>
                    <TableCell className="font-mono text-xs text-ink-muted">{project.slug}</TableCell>
                    <TableCell>{role ? tLoose("roles", role) : "—"}</TableCell>
                    <TableCell>
                      <Badge tone={project.status === "active" ? "success" : "neutral"}>{tLoose("status", project.status)}</Badge>
                    </TableCell>
                    <TableCell>
                      <DateTime value={project.created_at} />
                    </TableCell>
                    <TableCell>
                      <div className="flex justify-end gap-1">
                        {active ? null : (
                          <Button
                            variant="ghost"
                            size="sm"
                            aria-label={t("projects.switchTo", { name: project.name })}
                            onClick={() => selectProject(project.id)}
                          >
                            {t("projects.switch")}
                          </Button>
                        )}
                        <GatedButton
                          variant="ghost"
                          size="sm"
                          aria-label={t("projects.settingsFor", { name: project.name })}
                          allowed={mayManage}
                          reason={deniedReason("projects:manage", role)}
                          onClick={() => setRenaming(project)}
                        >
                          <Settings2 aria-hidden="true" />
                          {t("projects.settings")}
                        </GatedButton>
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
          {projects.data.has_more ? (
            <p className="border-t border-line px-3 py-2 text-xs text-ink-muted">{t("projects.firstOnly")}</p>
          ) : null}
        </div>
      )}

      <CreateProjectDialog open={creating} onOpenChange={setCreating} />
      <RenameProjectDialog project={renaming} onClose={() => setRenaming(null)} />
    </>
  );
}
