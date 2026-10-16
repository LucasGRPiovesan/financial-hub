import { useState } from "react";

import { Field } from "../../components/form-field";
import { Badge } from "../../components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../../components/ui/card";
import { Input } from "../../components/ui/input";
import { Select } from "../../components/ui/select";
import { useI18n } from "../../i18n";
import { flattenCustomers, useCustomers } from "../console/customers-api";
import { ListSkeleton, QueryErrorState } from "../console/states";
import { formatDecimalString } from "./decimal";
import { flattenMetrics, useMetrics, useUsageSummary, type UsageQuery } from "./metrics-api";

/** A date input value ("2026-10-01") as the local-midnight instant. */
function startOfDay(value: string): string | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) return null;
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])).toISOString();
}

function isoDay(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function defaultRange(): { from: string; to: string } {
  const now = new Date();
  return { from: isoDay(new Date(now.getFullYear(), now.getMonth(), 1)), to: isoDay(new Date(now.getFullYear(), now.getMonth() + 1, 1)) };
}

function aggregationKey(value: string): "sum" | "count" | "max" | "unique_count" {
  return value === "count" || value === "max" || value === "unique_count" ? value : "sum";
}

export function UsageSummaryPanel({ projectId }: { projectId: string }) {
  const { t, locale } = useI18n();
  const customers = flattenCustomers(useCustomers(projectId, null).data);
  const metrics = flattenMetrics(useMetrics(projectId).data);
  const [customerId, setCustomerId] = useState("");
  const [metric, setMetric] = useState("");
  const [range, setRange] = useState(defaultRange);

  const from = startOfDay(range.from);
  const to = startOfDay(range.to);
  const invalidRange = from !== null && to !== null && to <= from;
  const query: UsageQuery | null = customerId && metric && from && to && !invalidRange ? { customerId, metric, from, to } : null;
  const summary = useUsageSummary(projectId, query);

  let result;
  if (invalidRange) {
    result = (
      <p role="alert" className="text-sm text-danger">
        {t("billing.summary.invalidRange")}
      </p>
    );
  } else if (query === null) {
    result = <p className="text-sm text-ink-muted">{t("billing.summary.prompt")}</p>;
  } else if (summary.isPending) {
    result = <ListSkeleton rows={1} label={t("billing.summary.loading")} />;
  } else if (summary.isError) {
    result = <QueryErrorState title={t("billing.summary.failed")} error={summary.error} onRetry={() => void summary.refetch()} />;
  } else {
    const value = summary.data.value;
    result = (
      <div className="flex flex-wrap items-end gap-x-8 gap-y-3" aria-live="polite">
        <div>
          <p className="text-xs font-medium tracking-wide text-ink-muted uppercase">{t("billing.summary.value")}</p>
          <p className="figures mt-1 text-3xl font-semibold tracking-tight" data-testid="usage-value">
            {value === null ? t("billing.summary.noValue") : formatDecimalString(value, locale)}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 pb-1">
          <Badge tone="neutral">{t(`billing.metrics.aggregation.${aggregationKey(summary.data.aggregation)}`)}</Badge>
          <span className="text-sm text-ink-muted">{t("billing.summary.events", { count: summary.data.events })}</span>
        </div>
      </div>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("billing.summary.title")}</CardTitle>
        <CardDescription>{t("billing.summary.description")}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <Field label={t("billing.summary.customer")} hint={customers.length === 0 ? t("billing.summary.noCustomers") : undefined}>
            <Select value={customerId} onChange={(event) => setCustomerId(event.target.value)}>
              <option value="">{t("billing.summary.pickCustomer")}</option>
              {customers.map((customer) => (
                <option key={customer.id} value={customer.id}>
                  {customer.anonymized ? customer.external_ref : `${customer.name} (${customer.external_ref})`}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={t("billing.summary.metric")} hint={metrics.length === 0 ? t("billing.summary.noMetrics") : undefined}>
            <Select value={metric} onChange={(event) => setMetric(event.target.value)}>
              <option value="">{t("billing.summary.pickMetric")}</option>
              {metrics.map((item) => (
                <option key={item.code} value={item.code}>
                  {item.name} ({item.code})
                </option>
              ))}
            </Select>
          </Field>
          <Field label={t("billing.summary.from")}>
            <Input type="date" value={range.from} onChange={(event) => setRange({ ...range, from: event.target.value })} />
          </Field>
          <Field label={t("billing.summary.to")}>
            <Input type="date" value={range.to} onChange={(event) => setRange({ ...range, to: event.target.value })} />
          </Field>
        </div>
        <div className="border-t border-line pt-4">{result}</div>
      </CardContent>
    </Card>
  );
}
