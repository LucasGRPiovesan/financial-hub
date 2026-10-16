import { Plus, Search, UserRound, X } from "lucide-react";
import { useState, type FormEvent } from "react";

import type { CustomerOut } from "../api/generated/model";
import { DateTime } from "../components/date-time";
import { PageHeader } from "../components/page-header";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Sheet, SheetContent, DialogDescription, DialogTitle } from "../components/ui/dialog";
import { EmptyState } from "../components/ui/empty-state";
import { Input } from "../components/ui/input";
import { Select } from "../components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { AnonymizeCustomerDialog, CustomerFormDialog } from "../features/console/customer-dialogs";
import { kindLabel } from "../features/console/customer-kinds";
import { flattenCustomers, useCustomers, type CustomerFilter } from "../features/console/customers-api";
import { GatedButton } from "../features/console/gated-button";
import { NoProjectState } from "../features/console/no-project";
import { deniedReason } from "../features/console/permissions";
import { ListSkeleton, QueryErrorState } from "../features/console/states";
import { useProject } from "../features/console/use-project";

const DESCRIPTION = "People and companies your payments belong to, identified by your own reference.";

export function CustomersPage() {
  const { membership, can } = useProject();
  if (!membership) {
    return (
      <>
        <PageHeader title="Customers" description={DESCRIPTION} scope="implemented" />
        <NoProjectState />
      </>
    );
  }
  if (!can("customers:read")) {
    return (
      <>
        <PageHeader title="Customers" description={DESCRIPTION} scope="implemented" />
        <EmptyState icon={UserRound} title="No access to customers" description={deniedReason("customers:read", membership.role)} />
      </>
    );
  }
  return <CustomersForProject projectId={membership.project_id} role={membership.role} mayManage={can("customers:manage")} />;
}

function CustomersForProject({ projectId, role, mayManage }: { projectId: string; role: string; mayManage: boolean }) {
  const [filter, setFilter] = useState<CustomerFilter>(null);
  const [by, setBy] = useState<"external_ref" | "email">("external_ref");
  const [value, setValue] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<CustomerOut | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<CustomerOut | null>(null);
  const [anonymizing, setAnonymizing] = useState<CustomerOut | null>(null);

  const customers = useCustomers(projectId, filter);
  const rows = flattenCustomers(customers.data);
  // The drawer follows the list so it reflects an edit as soon as the refetch lands; the snapshot covers a row
  // that is no longer on the loaded pages.
  const selected = selectedId === null ? null : (rows.find((row) => row.id === selectedId) ?? snapshot);

  function applyFilter(event: FormEvent) {
    event.preventDefault();
    const trimmed = value.trim();
    setFilter(trimmed === "" ? null : { by, value: trimmed });
  }

  function clearFilter() {
    setValue("");
    setFilter(null);
  }

  function open(customer: CustomerOut) {
    setSelectedId(customer.id);
    setSnapshot(customer);
  }

  function startCreate() {
    setEditing(null);
    setFormOpen(true);
  }

  const header = (
    <PageHeader
      title="Customers"
      description={DESCRIPTION}
      scope="implemented"
      actions={
        <GatedButton allowed={mayManage} reason={deniedReason("customers:manage", role)} onClick={startCreate}>
          <Plus aria-hidden="true" />
          New customer
        </GatedButton>
      }
    />
  );

  const filterForm = (
    <form onSubmit={applyFilter} role="search" aria-label="Filter customers" className="mb-4 flex flex-wrap items-end gap-2">
      <div className="w-40">
        <label htmlFor="customer-filter-by" className="mb-1 block text-sm font-medium">
          Filter by
        </label>
        <Select
          id="customer-filter-by"
          value={by}
          onChange={(event) => setBy(event.target.value === "email" ? "email" : "external_ref")}
        >
          <option value="external_ref">External reference</option>
          <option value="email">Email</option>
        </Select>
      </div>
      <div className="min-w-56 flex-1">
        <label htmlFor="customer-filter-value" className="mb-1 block text-sm font-medium">
          Exact value
        </label>
        <Input
          id="customer-filter-value"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          autoComplete="off"
          spellCheck={false}
          placeholder={by === "email" ? "ana@example.com" : "cust_1042"}
        />
      </div>
      <Button type="submit" variant="secondary">
        <Search aria-hidden="true" />
        Apply filter
      </Button>
      {filter ? (
        <Button type="button" variant="ghost" onClick={clearFilter}>
          <X aria-hidden="true" />
          Clear filter
        </Button>
      ) : null}
    </form>
  );

  let body;
  if (customers.isPending) {
    body = <ListSkeleton label="Loading customers" />;
  } else if (customers.isError) {
    body = <QueryErrorState title="Couldn't load customers" error={customers.error} onRetry={() => void customers.refetch()} />;
  } else if (rows.length === 0) {
    body = filter ? (
      <EmptyState
        icon={Search}
        title="No customer matches"
        description={`Nothing has ${filter.by === "email" ? "this email" : "this external reference"}. Filters match the whole value, not part of it.`}
        action={
          <Button variant="secondary" onClick={clearFilter}>
            Clear filter
          </Button>
        }
      />
    ) : (
      <EmptyState
        icon={UserRound}
        title="No customers yet"
        description="Customers appear here when you create them in this screen or send them through the API."
        action={
          mayManage ? (
            <Button onClick={startCreate}>
              <Plus aria-hidden="true" />
              New customer
            </Button>
          ) : undefined
        }
      />
    );
  } else {
    body = (
      <>
        <div className="rounded-md border border-line bg-surface">
          <Table>
            <caption className="sr-only">Customers</caption>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>External reference</TableHead>
                <TableHead>Name</TableHead>
                <TableHead>Type</TableHead>
                <TableHead>Email</TableHead>
                <TableHead>Document</TableHead>
                <TableHead>Updated</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((customer) => (
                <TableRow key={customer.id}>
                  <TableCell>
                    <button
                      type="button"
                      onClick={() => open(customer)}
                      className="rounded-xs font-mono text-sm font-medium text-primary underline-offset-2 hover:underline"
                      aria-label={`Open ${customer.external_ref}`}
                    >
                      {customer.external_ref}
                    </button>
                  </TableCell>
                  <TableCell className={customer.anonymized ? "text-ink-muted italic" : undefined}>
                    {customer.anonymized ? "Anonymized" : customer.name}
                  </TableCell>
                  <TableCell>
                    <Badge tone="neutral">{kindLabel(customer.kind)}</Badge>
                  </TableCell>
                  <TableCell className="text-ink-muted">{customer.email ?? "-"}</TableCell>
                  <TableCell className="font-mono text-sm figures">{customer.document_masked ?? "-"}</TableCell>
                  <TableCell className="text-ink-muted">
                    <DateTime value={customer.updated_at} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
        <div className="mt-4 flex items-center justify-between gap-3">
          <p className="text-sm text-ink-muted" aria-live="polite">
            {rows.length} {rows.length === 1 ? "customer" : "customers"} loaded
            {customers.hasNextPage ? ", more available" : ""}
          </p>
          {customers.hasNextPage ? (
            <Button variant="secondary" onClick={() => void customers.fetchNextPage()} disabled={customers.isFetchingNextPage}>
              {customers.isFetchingNextPage ? "Loading…" : "Load more"}
            </Button>
          ) : null}
        </div>
        {customers.isFetchNextPageError ? (
          <p role="alert" className="mt-2 text-sm text-danger">
            Couldn't load the next page. Try again.
          </p>
        ) : null}
      </>
    );
  }

  return (
    <>
      {header}
      {filterForm}
      {body}

      <Sheet open={selected !== null} onOpenChange={(next) => (next ? undefined : setSelectedId(null))}>
        <SheetContent className="w-[min(32rem,calc(100vw-2rem))] overflow-y-auto p-6">
          {selected ? (
            <CustomerDetail
              customer={selected}
              mayManage={mayManage}
              role={role}
              onEdit={() => {
                setEditing(selected);
                setFormOpen(true);
              }}
              onAnonymize={() => setAnonymizing(selected)}
            />
          ) : null}
        </SheetContent>
      </Sheet>

      {formOpen ? (
        <CustomerFormDialog
          open
          customer={editing}
          projectId={projectId}
          onClose={() => setFormOpen(false)}
          onSaved={(saved) => {
            setFormOpen(false);
            open(saved);
          }}
        />
      ) : null}
      <AnonymizeCustomerDialog
        customer={anonymizing}
        projectId={projectId}
        onClose={() => setAnonymizing(null)}
        onDone={(done) => {
          setAnonymizing(null);
          setSnapshot(done);
        }}
      />
    </>
  );
}

function CustomerDetail({
  customer,
  mayManage,
  role,
  onEdit,
  onAnonymize,
}: {
  customer: CustomerOut;
  mayManage: boolean;
  role: string;
  onEdit: () => void;
  onAnonymize: () => void;
}) {
  const metadata = Object.entries(customer.metadata);
  const locked = !mayManage || customer.anonymized;
  const reason = customer.anonymized ? "An anonymized customer can't be changed." : deniedReason("customers:manage", role);
  return (
    <>
      <div className="flex flex-col gap-1 pr-8">
        <DialogTitle className="break-all font-mono text-base">{customer.external_ref}</DialogTitle>
        <DialogDescription>
          {customer.anonymized ? "Personal data was erased." : `${kindLabel(customer.kind)}, ${customer.name}`}
        </DialogDescription>
        {customer.anonymized ? (
          <div>
            <Badge tone="warning">Anonymized</Badge>
          </div>
        ) : null}
      </div>

      <dl className="grid grid-cols-[7rem_minmax(0,1fr)] gap-x-4 gap-y-3 text-sm">
        <Row label="Name" value={customer.anonymized ? null : customer.name} />
        <Row label="Type" value={kindLabel(customer.kind)} />
        <Row label="Document" value={customer.document_masked} mono />
        <Row label="Email" value={customer.email} />
        <Row label="Phone" value={customer.phone} />
        <div className="contents">
          <dt className="text-ink-muted">Created</dt>
          <dd>
            <DateTime value={customer.created_at} />
          </dd>
        </div>
        <div className="contents">
          <dt className="text-ink-muted">Updated</dt>
          <dd>
            <DateTime value={customer.updated_at} />
          </dd>
        </div>
      </dl>

      <section aria-labelledby="customer-metadata-heading">
        <h3 id="customer-metadata-heading" className="mb-2 text-sm font-semibold">
          Metadata
        </h3>
        {metadata.length === 0 ? (
          <p className="text-sm text-ink-muted">No metadata.</p>
        ) : (
          <dl className="grid grid-cols-[7rem_minmax(0,1fr)] gap-x-4 gap-y-2 rounded-sm border border-line bg-surface-sunken p-3 text-sm">
            {metadata.map(([key, item]) => (
              <div key={key} className="contents">
                <dt className="break-all font-mono text-xs text-ink-muted">{key}</dt>
                <dd className="break-all font-mono text-xs">{typeof item === "string" ? item : JSON.stringify(item)}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>

      <div className="mt-auto flex flex-wrap items-center gap-2 border-t border-line pt-4">
        <GatedButton allowed={!locked} reason={reason} onClick={onEdit}>
          Edit customer
        </GatedButton>
        <GatedButton allowed={!locked} reason={reason} variant="danger" onClick={onAnonymize}>
          Anonymize
        </GatedButton>
      </div>
    </>
  );
}

function Row({ label, value, mono }: { label: string; value: string | null; mono?: boolean }) {
  return (
    <div className="contents">
      <dt className="text-ink-muted">{label}</dt>
      <dd className={mono ? "font-mono" : "break-words"}>{value ?? <span className="text-ink-faint">Not set</span>}</dd>
    </div>
  );
}
