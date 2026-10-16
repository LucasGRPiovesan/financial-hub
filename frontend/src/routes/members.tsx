import { Lock, Plus, UserCog } from "lucide-react";
import { useState } from "react";

import type { MemberOut } from "../api/generated/model";
import { ConfirmDialog } from "../components/confirm-dialog";
import { DateTime } from "../components/date-time";
import { PageHeader } from "../components/page-header";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { EmptyState } from "../components/ui/empty-state";
import { Select } from "../components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { useToast } from "../components/ui/use-toast";
import { useSession } from "../features/auth/use-session";
import { errorText } from "../features/console/errors";
import { GatedButton } from "../features/console/gated-button";
import { AddMemberDialog } from "../features/console/member-dialogs";
import { useChangeMemberRole, useMembers, useRemoveMember } from "../features/console/members-api";
import { NoProjectState } from "../features/console/no-project";
import { deniedReason, isOwner } from "../features/console/permissions";
import { ROLES, ROLE_SUMMARY, roleTone } from "../features/console/roles";
import { ListSkeleton, QueryErrorState } from "../features/console/states";
import { useProject } from "../features/console/use-project";
import { isApiError } from "../lib/api";

/** Why a member change was refused, in the words an operator needs (403 owners only, 409 last owner, 422 bad input). */
function describeChangeFailure(error: unknown): string {
  if (isApiError(error)) {
    if (error.status === 403) return "Only an owner can grant, change or remove the owner role.";
    if (error.status === 409) return error.detail ?? "A project must keep at least one owner.";
    if (error.status === 422) return error.errors?.[0]?.detail ?? error.detail ?? "That change isn't valid.";
  }
  return errorText(error);
}

export function MembersPage() {
  const { membership, can } = useProject();
  if (!membership) {
    return (
      <>
        <PageHeader title="Members" description="Who can access this project, and what each role allows." />
        <NoProjectState />
      </>
    );
  }
  return <MembersForProject projectId={membership.project_id} role={membership.role} mayManage={can("members:manage")} />;
}

function MembersForProject({ projectId, role, mayManage }: { projectId: string; role: string; mayManage: boolean }) {
  const toast = useToast();
  const session = useSession();
  const members = useMembers(projectId);
  const changeRole = useChangeMemberRole(projectId);
  const removeMember = useRemoveMember(projectId);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<MemberOut | null>(null);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const iAmOwner = isOwner(role);

  function onRoleChange(member: MemberOut, next: string) {
    changeRole.mutate(
      { operatorId: member.operator_id, role: next },
      {
        onSuccess: () => toast({ tone: "success", title: "Role changed", description: `${member.display_name} is now ${next}.` }),
        onError: (error) => toast({ tone: "danger", title: "Couldn't change the role", description: describeChangeFailure(error) }),
      },
    );
  }

  function confirmRemove() {
    if (!removing) return;
    setRemoveError(null);
    const target = removing;
    removeMember.mutate(target.operator_id, {
      onSuccess: () => {
        setRemoving(null);
        toast({ tone: "success", title: "Member removed", description: `${target.display_name} no longer has access.` });
      },
      onError: (error) => setRemoveError(describeChangeFailure(error)),
    });
  }

  const header = (
    <PageHeader
      title="Members"
      description="Who can access this project, and what each role allows."
      actions={
        <GatedButton allowed={mayManage} reason={deniedReason("members:manage", role)} onClick={() => setAdding(true)}>
          <Plus aria-hidden="true" />
          Add member
        </GatedButton>
      }
    />
  );

  if (members.isPending) {
    return (
      <>
        {header}
        <ListSkeleton label="Loading members" />
      </>
    );
  }
  if (members.isError) {
    return (
      <>
        {header}
        <QueryErrorState title="Couldn't load members" error={members.error} onRetry={() => void members.refetch()} />
      </>
    );
  }

  const rows = members.data.data;
  return (
    <>
      {header}
      {!mayManage ? (
        <p className="mb-4 flex items-center gap-2 text-sm text-ink-muted">
          <Lock className="size-4 shrink-0" aria-hidden="true" />
          {deniedReason("members:manage", role)}
        </p>
      ) : null}

      {rows.length === 0 ? (
        <EmptyState icon={UserCog} title="No members yet" description="Add the operators who should work in this project." />
      ) : (
        <div className="rounded-md border border-line bg-surface">
          <Table>
            <caption className="sr-only">Project members</caption>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Member</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Added</TableHead>
                <TableHead>
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((member) => {
                const isSelf = member.operator_id === session.operator.id;
                // Anything touching an owner (their row, or the owner option) is for owners only.
                const locked = !mayManage || (member.role === "owner" && !iAmOwner);
                return (
                  <TableRow key={member.operator_id}>
                    <TableCell>
                      <p className="font-medium">
                        {member.display_name}
                        {isSelf ? <span className="ml-2 text-xs font-normal text-ink-muted">(you)</span> : null}
                      </p>
                      <p className="font-mono text-xs text-ink-muted">{member.email}</p>
                    </TableCell>
                    <TableCell>
                      {locked ? (
                        <Badge tone={roleTone(member.role)}>{member.role}</Badge>
                      ) : (
                        <div className="w-36">
                          <Select
                            aria-label={`Role for ${member.display_name}`}
                            value={member.role}
                            disabled={changeRole.isPending}
                            onChange={(event) => onRoleChange(member, event.target.value)}
                          >
                            {ROLES.map((item) => (
                              <option key={item} value={item} disabled={item === "owner" && !iAmOwner}>
                                {item}
                              </option>
                            ))}
                          </Select>
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="text-ink-muted">
                      <DateTime value={member.added_at} />
                    </TableCell>
                    <TableCell className="text-right">
                      {locked ? null : (
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label={`Remove ${member.display_name}`}
                          onClick={() => {
                            setRemoveError(null);
                            setRemoving(member);
                          }}
                        >
                          Remove
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

      <section aria-labelledby="roles-heading" className="mt-8">
        <h2 id="roles-heading" className="text-base font-semibold tracking-tight">
          What each role can do
        </h2>
        <dl className="mt-3 grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[8rem_minmax(0,1fr)]">
          {ROLES.map((item) => (
            <div key={item} className="contents">
              <dt>
                <Badge tone={roleTone(item)}>{item}</Badge>
              </dt>
              <dd className="text-ink-muted">{ROLE_SUMMARY[item]}</dd>
            </div>
          ))}
        </dl>
      </section>

      <AddMemberDialog open={adding} onOpenChange={setAdding} projectId={projectId} mayGrantOwner={iAmOwner} />
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => (open ? undefined : setRemoving(null))}
        title="Remove member"
        description={
          removing ? `${removing.display_name} (${removing.email}) will lose access to this project immediately.` : ""
        }
        confirmLabel="Remove member"
        pending={removeMember.isPending}
        error={removeError}
        onConfirm={confirmRemove}
      />
    </>
  );
}
