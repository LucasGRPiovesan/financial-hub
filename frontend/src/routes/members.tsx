import { UserPlus } from "lucide-react";

import { PageHeader } from "../components/page-header";
import { Badge } from "../components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { EmptyState } from "../components/ui/empty-state";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { NoProjectState } from "../features/console/no-project";
import { deniedReason } from "../features/console/permissions";
import { useProject } from "../features/console/use-project";

const ROLES: readonly { role: string; can: string }[] = [
  { role: "owner", can: "Everything, including members, API keys, provider accounts and project settings." },
  { role: "admin", can: "Members, API keys, provider accounts and project settings." },
  { role: "finance", can: "Read the ledger and provider accounts." },
  { role: "viewer", can: "Read the ledger and provider accounts." },
];

/**
 * The API has no endpoint to list, add, change or remove project members yet (only the operator's own
 * memberships on `/v1/auth/me`), so this page shows the operator's role and what each role can do.
 */
export function MembersPage() {
  const { membership, can } = useProject();
  if (!membership) return <NoProjectState />;
  const mayManage = can("members:manage");

  return (
    <>
      <PageHeader
        title="Members"
        description="Who can access this project, and what each role allows."
        scope="planned"
      />
      <div className="flex flex-col gap-6">
        <Card>
          <CardHeader>
            <CardTitle>Your access</CardTitle>
            <CardDescription>
              You are <Badge tone="primary">{membership.role}</Badge> in this project.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-ink-muted">
              {mayManage
                ? "Your role can manage members once the API supports it."
                : deniedReason("members:manage", membership.role)}
            </p>
          </CardContent>
        </Card>

        <EmptyState
          icon={UserPlus}
          title="Member management isn't available yet"
          status={<Badge tone="neutral">Planned</Badge>}
          description="Listing, adding, changing and removing members needs API endpoints that don't exist yet."
        />

        <div className="rounded-md border border-line bg-surface">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>Role</TableHead>
                <TableHead>Can</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {ROLES.map((item) => (
                <TableRow key={item.role}>
                  <TableCell className="font-medium">{item.role}</TableCell>
                  <TableCell className="text-ink-muted">{item.can}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </div>
    </>
  );
}
