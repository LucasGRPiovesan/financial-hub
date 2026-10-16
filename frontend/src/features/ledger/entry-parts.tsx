import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

import type { PostingOut } from "../../api/generated/model";
import { Money } from "../../components/money";
import { Badge } from "../../components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../../components/ui/table";
import { useI18n } from "../../i18n";
import { splitAccountCode, useFamilyName } from "./ledger-labels";
import { entryTone, minor, totalsByCurrency } from "./ledger-model";

/** An account code: the family name with the owner reference (customer, payee, provider account) underneath. */
export function AccountLabel({ code }: { code: string }) {
  const familyName = useFamilyName();
  const { family, owner } = splitAccountCode(code);
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="font-medium">{familyName(family)}</span>
      {owner ? (
        <span className="max-w-[16rem] truncate font-mono text-xs text-ink-muted" title={owner}>
          {owner}
        </span>
      ) : (
        <span className="font-mono text-xs text-ink-faint">{family}</span>
      )}
    </div>
  );
}

/** The recipe code with its readable name, in the active language. */
export function RecipeLabel({ recipe }: { recipe: string }) {
  const { tLoose } = useI18n();
  return (
    <span className="inline-flex flex-wrap items-baseline gap-x-1.5">
      <span className="font-mono text-xs font-semibold">{recipe}</span>
      <span>{tLoose("ledger.recipe", recipe, recipe)}</span>
    </span>
  );
}

export function EntryStatusBadge({ status }: { status: string }) {
  const { tLoose } = useI18n();
  return <Badge tone={entryTone(status)}>{tLoose("ledger.status", status, status)}</Badge>;
}

export function Fact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-ink-muted">{label}</dt>
      <dd className="mt-0.5">{value}</dd>
    </div>
  );
}

export function EntryLink({ entryId, label }: { entryId: string; label: string }) {
  return (
    <Link
      to="/ledger/entries/$entryId"
      params={{ entryId }}
      aria-label={label}
      className="rounded-xs text-sm font-medium text-primary underline-offset-2 hover:underline"
    >
      {entryId}
    </Link>
  );
}

/** Postings with separate Debit and Credit columns and a totals row per currency that shows the entry balances. */
export function PostingsTable({ postings }: { postings: readonly PostingOut[] }) {
  const { t } = useI18n();
  const totals = totalsByCurrency(postings);
  return (
    <Table>
      <caption className="sr-only">{t("ledger.detail.postingsCaption")}</caption>
      <TableHeader>
        <TableRow className="hover:bg-transparent">
          <TableHead>{t("ledger.detail.cols.account")}</TableHead>
          <TableHead numeric>{t("ledger.detail.cols.debit")}</TableHead>
          <TableHead numeric>{t("ledger.detail.cols.credit")}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {postings.map((posting) => {
          const debit = posting.direction === "debit";
          const value = minor(posting.amount.amount_minor, posting.amount.currency);
          return (
            <TableRow key={posting.id}>
              <TableCell>
                <AccountLabel code={posting.account_code} />
              </TableCell>
              <TableCell numeric data-testid="posting-debit">
                {debit ? <Money value={value} /> : <span className="text-ink-faint">&mdash;</span>}
              </TableCell>
              <TableCell numeric data-testid="posting-credit">
                {debit ? <span className="text-ink-faint">&mdash;</span> : <Money value={value} />}
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
      <tfoot>
        {totals.map((entry) => (
          <TableRow key={entry.currency} className="border-t-2 border-line-strong bg-surface-sunken font-semibold hover:bg-surface-sunken">
            <TableCell>
              <span className="flex flex-wrap items-center gap-2">
                {t("ledger.detail.totals", { currency: entry.currency })}
                <Badge tone={entry.debits === entry.credits ? "success" : "danger"}>
                  {t(entry.debits === entry.credits ? "ledger.detail.balancedOk" : "ledger.detail.balancedBad")}
                </Badge>
              </span>
            </TableCell>
            <TableCell numeric data-testid="total-debits">
              <Money value={minor(entry.debits, entry.currency)} />
            </TableCell>
            <TableCell numeric data-testid="total-credits">
              <Money value={minor(entry.credits, entry.currency)} />
            </TableCell>
          </TableRow>
        ))}
      </tfoot>
    </Table>
  );
}
