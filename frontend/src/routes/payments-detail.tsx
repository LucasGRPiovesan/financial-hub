import { Link, useParams } from "@tanstack/react-router";
import { ArrowLeft, Undo2, WalletCards } from "lucide-react";
import { useState, type ReactNode } from "react";

import type { PaymentOut, RefundOut } from "../api/generated/model";
import { DateTime } from "../components/date-time";
import { Money } from "../components/money";
import { PageHeader } from "../components/page-header";
import { formatCalendarDate } from "../lib/datetime";
import { Badge } from "../components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { EmptyState } from "../components/ui/empty-state";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { GatedButton } from "../features/console/gated-button";
import { NoProjectState } from "../features/console/no-project";
import { deniedReason } from "../features/console/permissions";
import { ListSkeleton, QueryErrorState } from "../features/console/states";
import { useProject } from "../features/console/use-project";
import { CopyableId, MethodBadge, StatusBadge } from "../features/payments/payment-parts";
import { moneyOf, refundTone, secondaryStates, statusKey } from "../features/payments/payment-status";
import { useCustomerLookup, usePayment } from "../features/payments/payments-api";
import { RefundDialog } from "../features/payments/refund-dialog";
import { useDocumentTitle } from "../features/shell/document-title";
import { useI18n } from "../i18n";
import { isApiError } from "../lib/api";
import { formatMoney } from "../lib/money";

// Screens registered from the SCREENS table are not in the router's static route types, so these paths are plain strings (as in the sidebar).
const PAYMENTS_PATH: string = "/payments";
const CUSTOMERS_PATH: string = "/customers";

export function PaymentDetailPage() {
  const { t } = useI18n();
  const { membership, can } = useProject();
  const { paymentId } = useParams({ strict: false });
  useDocumentTitle(t("payments.title"));
  if (!membership) return <NoProjectState />;
  if (!can("payments:read")) {
    return <EmptyState icon={WalletCards} title={t("payments.noAccess")} description={deniedReason("payments:read", membership.role)} />;
  }
  if (!paymentId) return null;
  return <PaymentDetail projectId={membership.project_id} paymentId={paymentId} role={membership.role} mayRefund={can("payments:refund")} />;
}

function BackLink() {
  const { t } = useI18n();
  return (
    <Link to={PAYMENTS_PATH} className="mb-3 inline-flex items-center gap-1 rounded-xs text-sm text-primary underline-offset-2 hover:underline">
      <ArrowLeft className="size-4" aria-hidden="true" />
      {t("payments.detail.back")}
    </Link>
  );
}

function PaymentDetail({ projectId, paymentId, role, mayRefund }: { projectId: string; paymentId: string; role: string; mayRefund: boolean }) {
  const { t } = useI18n();
  const query = usePayment(projectId, paymentId);
  if (query.isPending) {
    return (
      <>
        <BackLink />
        <ListSkeleton label={t("payments.detail.loading")} />
      </>
    );
  }
  if (query.isError) {
    if (isApiError(query.error) && query.error.status === 404) {
      return (
        <>
          <BackLink />
          <EmptyState icon={WalletCards} title={t("payments.detail.notFoundTitle")} description={t("payments.detail.notFoundDescription")} />
        </>
      );
    }
    return (
      <>
        <BackLink />
        <QueryErrorState title={t("payments.detail.loadFailed")} error={query.error} onRetry={() => void query.refetch()} />
      </>
    );
  }
  return <PaymentView payment={query.data} role={role} mayRefund={mayRefund} />;
}

function PaymentView({ payment, role, mayRefund }: { payment: PaymentOut; role: string; mayRefund: boolean }) {
  const { t, tLoose } = useI18n();
  const [refunding, setRefunding] = useState(false);
  const customers = useCustomerLookup(payment.project_id, [payment.customer_id]);
  const customer = customers.get(payment.customer_id);
  const money = (amount: number) => moneyOf(amount, payment.currency);
  const net = payment.captured_amount - payment.refunded_amount;
  const nothingToRefund = payment.refundable_amount <= 0;
  const reason = !mayRefund ? deniedReason("payments:refund", role) : nothingToRefund ? t("payments.refund.nothingRefundable") : "";

  return (
    <>
      <BackLink />
      <PageHeader
        title={t("payments.detail.title")}
        description={payment.description ?? undefined}
        actions={
          <GatedButton allowed={mayRefund && !nothingToRefund} reason={reason} onClick={() => setRefunding(true)}>
            <Undo2 aria-hidden="true" />
            {t("payments.refund.action")}
          </GatedButton>
        }
      />

      <div className="mb-6 flex flex-wrap items-center gap-3">
        <CopyableId id={payment.id} truncate={false} />
        <MethodBadge method={payment.method} />
        <StatusBadge status={payment.status} />
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        <div className="flex flex-col gap-4 xl:col-span-2">
          <Card>
            <CardHeader>
              <CardTitle>{t("payments.detail.amounts")}</CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
                <Fact label={t("payments.detail.gross")} value={<Money value={money(payment.amount)} className="text-lg font-semibold" />} />
                <Fact label={t("payments.detail.captured")} value={<Money value={money(payment.captured_amount)} />} />
                <Fact label={t("payments.detail.refunded")} value={<Money value={money(payment.refunded_amount)} />} />
                <Fact label={t("payments.detail.net")} value={<Money value={money(net)} />} />
                <Fact label={t("payments.detail.refundable")} value={<Money value={money(payment.refundable_amount)} />} />
              </dl>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("payments.detail.states")}</CardTitle>
            </CardHeader>
            <CardContent>
              <ul className="flex flex-wrap gap-2">
                {secondaryStates(payment).map((state) => (
                  <li key={state.field}>
                    <Badge tone="neutral">
                      {t(`payments.field.${state.field}`)}: {tLoose("payments.state", statusKey(state.value), state.value)}
                    </Badge>
                  </li>
                ))}
              </ul>
              {payment.failure_code ? (
                <p className="mt-3 text-sm text-danger">
                  {t("payments.detail.failureCode")}: <span className="font-mono">{payment.failure_code}</span>
                </p>
              ) : null}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("payments.detail.refunds")}</CardTitle>
            </CardHeader>
            <CardContent>
              {payment.refunds.length === 0 ? (
                <p className="text-sm text-ink-muted">{t("payments.detail.noRefunds")}</p>
              ) : (
                <Table>
                  <caption className="sr-only">{t("payments.detail.refunds")}</caption>
                  <TableHeader>
                    <TableRow className="hover:bg-transparent">
                      <TableHead className="text-right">{t("payments.detail.refundCols.amount")}</TableHead>
                      <TableHead>{t("payments.detail.refundCols.status")}</TableHead>
                      <TableHead>{t("payments.detail.refundCols.origin")}</TableHead>
                      <TableHead>{t("payments.detail.refundCols.reason")}</TableHead>
                      <TableHead>{t("payments.detail.refundCols.created")}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {payment.refunds.map((refund) => (
                      <RefundRow key={refund.id} refund={refund} />
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </div>

        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader>
              <CardTitle>{t("payments.detail.customer")}</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-1 text-sm">
              <span className="font-medium">
                {customer ? (customer.anonymized ? t("payments.anonymizedCustomer") : customer.name) : t("payments.unknownCustomer")}
              </span>
              {customer && !customer.anonymized && customer.email ? <span className="text-ink-muted">{customer.email}</span> : null}
              <span className="font-mono text-xs text-ink-muted">{customer?.external_ref ?? payment.customer_id}</span>
              <Link to={CUSTOMERS_PATH} className="mt-1 w-fit rounded-xs text-primary underline-offset-2 hover:underline">
                {t("payments.detail.viewCustomers")}
              </Link>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("payments.detail.provider")}</CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="flex flex-col gap-3">
                <Fact label={t("payments.detail.providerName")} value={<span className="font-mono text-sm">{payment.provider}</span>} />
                <Fact
                  label={t("payments.detail.providerPaymentId")}
                  value={<span className="font-mono text-sm break-all">{payment.provider_payment_id ?? t("payments.detail.notSet")}</span>}
                />
                <Fact
                  label={t("payments.detail.providerAccount")}
                  value={<span className="font-mono text-sm break-all">{payment.provider_account_id ?? t("payments.detail.notSet")}</span>}
                />
                <Fact label={t("payments.detail.externalRef")} value={<span className="font-mono text-sm break-all">{payment.external_ref ?? t("payments.detail.notSet")}</span>} />
                <Fact label={t("payments.detail.dueDate")} value={<span className="figures">{payment.due_date ? formatCalendarDate(payment.due_date) : t("payments.detail.notSet")}</span>} />
              </dl>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("payments.detail.timeline")}</CardTitle>
            </CardHeader>
            <CardContent>
              <Timeline payment={payment} />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("payments.detail.metadata")}</CardTitle>
            </CardHeader>
            <CardContent>
              {Object.keys(payment.metadata).length === 0 ? (
                <p className="text-sm text-ink-muted">{t("payments.detail.noMetadata")}</p>
              ) : (
                <pre className="overflow-x-auto rounded-sm bg-surface-sunken p-3 font-mono text-xs">{JSON.stringify(payment.metadata, null, 2)}</pre>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      <RefundDialog payment={payment} open={refunding} onClose={() => setRefunding(false)} />
    </>
  );
}

function Fact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-ink-muted">{label}</dt>
      <dd className="mt-0.5">{value}</dd>
    </div>
  );
}

function RefundRow({ refund }: { refund: RefundOut }) {
  const { tLoose } = useI18n();
  return (
    <TableRow>
      <TableCell className="text-right">
        <Money value={moneyOf(refund.amount, refund.currency)} />
      </TableCell>
      <TableCell>
        <Badge tone={refundTone(refund.status)}>{tLoose("payments.refundStatus", refund.status, refund.status)}</Badge>
      </TableCell>
      <TableCell className="font-mono text-xs">{refund.origin}</TableCell>
      <TableCell className="text-ink-muted">{refund.reason ?? "-"}</TableCell>
      <TableCell className="text-ink-muted">
        <DateTime value={refund.created_at} />
      </TableCell>
    </TableRow>
  );
}

interface TimelineEntry {
  key: string;
  at: string;
  label: string;
}

/** The API exposes no per-state history, so the timeline is what the record itself dates: creation, refunds, last update. */
function Timeline({ payment }: { payment: PaymentOut }) {
  const { t, locale } = useI18n();
  const entries: TimelineEntry[] = [
    { key: "created", at: payment.created_at, label: t("payments.detail.timelineCreated") },
    ...payment.refunds.map((refund) => ({
      key: refund.id,
      at: refund.created_at,
      label: t("payments.detail.timelineRefund", { amount: formatMoney(moneyOf(refund.amount, payment.currency), { locale }) }),
    })),
  ];
  if (payment.updated_at !== payment.created_at) entries.push({ key: "updated", at: payment.updated_at, label: t("payments.detail.timelineUpdated") });
  entries.sort((a, b) => a.at.localeCompare(b.at));
  return (
    <ol className="flex flex-col gap-3 border-l border-line pl-4">
      {entries.map((entry) => (
        <li key={entry.key} className="relative text-sm">
          <span aria-hidden="true" className="absolute top-1.5 -left-[1.3rem] size-2 rounded-full bg-primary" />
          <div>{entry.label}</div>
          <DateTime value={entry.at} className="text-xs text-ink-muted" />
        </li>
      ))}
    </ol>
  );
}
