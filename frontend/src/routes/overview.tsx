import { PageHeader } from "../components/page-header";
import { ScopeBadge } from "../components/scope-badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { HealthIndicator } from "../features/health/health-indicator";
import { SCOPE_DESCRIPTIONS, SCOPES } from "../lib/scope";

export function OverviewPage() {
  return (
    <>
      <PageHeader
        title="Overview"
        description="Status of this environment and how to read the capability labels used throughout the console."
        scope="implemented"
      />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
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
            <CardTitle>Capability labels</CardTitle>
            <CardDescription>Every feature says how real it is.</CardDescription>
          </CardHeader>
          <CardContent className="px-2 py-2">
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>Label</TableHead>
                  <TableHead>Meaning</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {SCOPES.map((scope) => (
                  <TableRow key={scope}>
                    <TableCell>
                      <ScopeBadge scope={scope} />
                    </TableCell>
                    <TableCell className="text-ink-muted">{SCOPE_DESCRIPTIONS[scope]}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </>
  );
}
