import { Link } from "@tanstack/react-router";
import { Search, WalletCards, X } from "lucide-react";
import { useState } from "react";

import type { CustomerOut, PaymentOut } from "../api/generated/model";
import { DateTime } from "../components/date-time";
import { Money } from "../components/money";
import { PageHeader } from "../components/page-header";
import { Button } from "../components/ui/button";
import { EmptyState } from "../components/ui/empty-state";
import { Input } from "../components/ui/input";
import { Select } from "../components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { NoProjectState } from "../features/console/no-project";
import { deniedReason } from "../features/console/permissions";
import { ListSkeleton, QueryErrorState } from "../features/console/states";
import { useProject } from "../features/console/use-project";
import { CopyableId, MethodBadge, StatusBadge } from "../features/payments/payment-parts";
import { moneyOf, PAYMENT_METHODS, PAYMENT_STATUSES, statusKey } from "../features/payments/payment-status";
import { flattenPayments, useCustomerLookup, usePayments } from "../features/payments/payments-api";
import { useDocumentTitle } from "../features/shell/document-title";
import { useI18n } from "../i18n";

export function PaymentsPage() {
  const { t } = useI18n();
  const { membership, can } = useProject();
  useDocumentTitle(t("payments.title"));
  if (!membership) {
    return (
      <>
        <PageHeader title={t("payments.title")} description={t("payments.description")} />
        <NoProjectState />
      </>
    );
  }
  if (!can("payments:read")) {
    return (
      <>
        <PageHeader title={t("payments.title")} description={t("payments.description")} />
        <EmptyState icon={WalletCards} title={t("payments.noAccess")} description={deniedReason("payments:read", membership.role)} />
      </>
    );
  }
  return <PaymentsForProject projectId={membership.project_id} />;
}

interface Filters {
  text: string;
  status: string;
  method: string;
}

const NO_FILTERS: Filters = { text: "", status: "", method: "" };

function customerName(customer: CustomerOut | undefined, anonymized: string): string | null {
  if (!customer) return null;
  return customer.anonymized ? anonymized : customer.name;
}

function PaymentsForProject({ projectId }: { projectId: string }) {
  const { t } = useI18n();
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const payments = usePayments(projectId);
  const rows = flattenPayments(payments.data);
  const customers = useCustomerLookup(
    projectId,
    rows.map((row) => row.customer_id),
  );

  const needle = filters.text.trim().toLowerCase();
  const visible = rows.filter((row) => {
    if (filters.status && row.status !== filters.status) return false;
    if (filters.method && row.method !== filters.method) return false;
    if (needle === "") return true;
    const customer = customers.get(row.customer_id);
    const haystack = [row.id, row.external_ref, row.provider_payment_id, customer?.name, customer?.external_ref, customer?.email]
      .filter((part): part is string => typeof part === "string")
      .join(" ")
      .toLowerCase();
    return haystack.includes(needle);
  });
  const filtering = filters.text !== "" || filters.status !== "" || filters.method !== "";

  const filterBar = (
    <form
      role="search"
      aria-label={t("payments.filters.label")}
      onSubmit={(event) => event.preventDefault()}
      className="mb-4 flex flex-wrap items-end gap-2"
    >
      <div className="min-w-56 flex-1">
        <label htmlFor="payment-filter-text" className="mb-1 block text-sm font-medium">
          {t("payments.filters.search")}
        </label>
        <Input
          id="payment-filter-text"
          value={filters.text}
          onChange={(event) => setFilters({ ...filters, text: event.target.value })}
          autoComplete="off"
          spellCheck={false}
          placeholder={t("payments.filters.searchPlaceholder")}
        />
      </div>
      <div className="w-full sm:w-48">
        <label htmlFor="payment-filter-status" className="mb-1 block text-sm font-medium">
          {t("payments.filters.status")}
        </label>
        <Select id="payment-filter-status" value={filters.status} onChange={(event) => setFilters({ ...filters, status: event.target.value })}>
          <option value="">{t("payments.filters.allStatuses")}</option>
          {PAYMENT_STATUSES.map((status) => (
            <option key={status} value={status}>
              {t(`payments.status.${statusKey(status) as "settled"}`)}
            </option>
          ))}
        </Select>
      </div>
      <div className="w-full sm:w-48">
        <label htmlFor="payment-filter-method" className="mb-1 block text-sm font-medium">
          {t("payments.filters.method")}
        </label>
        <Select id="payment-filter-method" value={filters.method} onChange={(event) => setFilters({ ...filters, method: event.target.value })}>
          <option value="">{t("payments.filters.allMethods")}</option>
          {PAYMENT_METHODS.map((method) => (
            <option key={method} value={method}>
              {t(`payments.method.${method}`)}
            </option>
          ))}
        </Select>
      </div>
      {filtering ? (
        <Button type="button" variant="ghost" onClick={() => setFilters(NO_FILTERS)}>
          <X aria-hidden="true" />
          {t("payments.filters.clear")}
        </Button>
      ) : null}
    </form>
  );

  let body;
  if (payments.isPending) {
    body = <ListSkeleton label={t("payments.loading")} />;
  } else if (payments.isError) {
    body = <QueryErrorState title={t("payments.loadFailed")} error={payments.error} onRetry={() => void payments.refetch()} />;
  } else if (rows.length === 0) {
    body = <EmptyState icon={WalletCards} title={t("payments.emptyTitle")} description={t("payments.emptyDescription")} />;
  } else {
    body = (
      <>
        {visible.length === 0 ? (
          <EmptyState
            icon={Search}
            title={t("payments.filters.noMatchTitle")}
            description={t("payments.filters.noMatchDescription")}
            action={
              <Button variant="secondary" onClick={() => setFilters(NO_FILTERS)}>
                {t("payments.filters.clear")}
              </Button>
            }
          />
        ) : (
          <div className="rounded-md border border-line bg-surface">
            <Table>
              <caption className="sr-only">{t("payments.caption")}</caption>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>{t("payments.cols.id")}</TableHead>
                  <TableHead>{t("payments.cols.customer")}</TableHead>
                  <TableHead>{t("payments.cols.method")}</TableHead>
                  <TableHead className="text-right">{t("payments.cols.amount")}</TableHead>
                  <TableHead>{t("payments.cols.status")}</TableHead>
                  <TableHead>{t("payments.cols.created")}</TableHead>
                  <TableHead>
                    <span className="sr-only">{t("payments.cols.actions")}</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visible.map((payment) => (
                  <PaymentRow key={payment.id} payment={payment} customer={customers.get(payment.customer_id)} />
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-ink-muted" aria-live="polite">
            {t(payments.hasNextPage ? "payments.loadedMore" : "payments.loaded", { count: rows.length })}
            {filtering ? ` · ${t("payments.filters.scopeNote")}` : null}
          </p>
          {payments.hasNextPage ? (
            <Button variant="secondary" onClick={() => void payments.fetchNextPage()} disabled={payments.isFetchingNextPage}>
              {payments.isFetchingNextPage ? t("payments.loadingMore") : t("payments.loadMore")}
            </Button>
          ) : null}
        </div>
        {payments.isFetchNextPageError ? (
          <p role="alert" className="mt-2 text-sm text-danger">
            {t("payments.nextPageFailed")}
          </p>
        ) : null}
      </>
    );
  }

  return (
    <>
      <PageHeader title={t("payments.title")} description={t("payments.description")} />
      {filterBar}
      {body}
    </>
  );
}

function PaymentRow({ payment, customer }: { payment: PaymentOut; customer: CustomerOut | undefined }) {
  const { t } = useI18n();
  const name = customerName(customer, t("payments.anonymizedCustomer"));
  return (
    <TableRow>
      <TableCell>
        <div className="flex flex-col gap-0.5">
          <CopyableId id={payment.id} />
          {payment.external_ref ? <span className="text-xs text-ink-muted">{payment.external_ref}</span> : null}
        </div>
      </TableCell>
      <TableCell>{name ?? <span className="text-ink-muted">{t("payments.unknownCustomer")}</span>}</TableCell>
      <TableCell>
        <MethodBadge method={payment.method} />
      </TableCell>
      <TableCell className="text-right">
        <Money value={moneyOf(payment.amount, payment.currency)} />
      </TableCell>
      <TableCell>
        <PaymentStatusCell payment={payment} />
      </TableCell>
      <TableCell className="text-ink-muted">
        <DateTime value={payment.created_at} />
      </TableCell>
      <TableCell className="text-right">
        <Link
          to="/payments/$paymentId"
          params={{ paymentId: payment.id }}
          aria-label={t("payments.openNamed", { id: payment.id })}
          className="rounded-xs text-sm font-medium text-primary underline-offset-2 hover:underline"
        >
          {t("payments.open")}
        </Link>
      </TableCell>
    </TableRow>
  );
}

/** Primary status plus, when the underlying fields add information, a short secondary line. */
function PaymentStatusCell({ payment }: { payment: PaymentOut }) {
  const { t } = useI18n();
  return (
    <div className="flex flex-col items-start gap-0.5">
      <StatusBadge status={payment.status} />
      {payment.refunded_amount > 0 ? (
        <span className="text-xs text-ink-muted">
          {t("payments.refundedSecondary")} <Money value={moneyOf(payment.refunded_amount, payment.currency)} />
        </span>
      ) : null}
    </div>
  );
}
