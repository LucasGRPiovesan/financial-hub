import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { BookOpenText, Search, X } from "lucide-react";
import { useState } from "react";

import type { AccountOut, AccountType, EntryOut, EntryStatus, TrialBalanceLineOut } from "../api/generated/model";
import { DateTime } from "../components/date-time";
import { Money } from "../components/money";
import { PageHeader } from "../components/page-header";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { EmptyState } from "../components/ui/empty-state";
import { Input } from "../components/ui/input";
import { Select } from "../components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { useTrialBalance } from "../features/console/api";
import { NoProjectState } from "../features/console/no-project";
import { deniedReason } from "../features/console/permissions";
import { ListSkeleton, QueryErrorState } from "../features/console/states";
import { useProject } from "../features/console/use-project";
import { AccountLabel, EntryStatusBadge, RecipeLabel } from "../features/ledger/entry-parts";
import { splitAccountCode, useFamilyName } from "../features/ledger/ledger-labels";
import { flattenAccounts, flattenEntries, useAccounts, useEntries, useEntry } from "../features/ledger/ledger-api";
import { ENTRY_STATUSES, FILTER_RECIPES, minor } from "../features/ledger/ledger-model";
import { CopyableId } from "../features/payments/payment-parts";
import { useDocumentTitle } from "../features/shell/document-title";
import { useI18n } from "../i18n";

const TABS = ["trial", "accounts", "entries"] as const;
type Tab = (typeof TABS)[number];
const ACCOUNT_TYPES: readonly AccountType[] = ["asset", "liability", "income", "expense", "equity"];

function isTab(value: unknown): value is Tab {
  return TABS.some((tab) => tab === value);
}

export function LedgerPage() {
  const { t } = useI18n();
  const { membership, can } = useProject();
  useDocumentTitle(t("ledger.title"));
  if (!membership) {
    return (
      <>
        <PageHeader title={t("ledger.title")} description={t("ledger.description")} />
        <NoProjectState />
      </>
    );
  }
  if (!can("ledger:read")) {
    return (
      <>
        <PageHeader title={t("ledger.title")} description={t("ledger.description")} />
        <EmptyState icon={BookOpenText} title={t("ledger.noAccess")} description={deniedReason("ledger:read", membership.role)} />
      </>
    );
  }
  return <LedgerForProject projectId={membership.project_id} />;
}

function LedgerForProject({ projectId }: { projectId: string }) {
  const { t } = useI18n();
  const search: Record<string, unknown> = useSearch({ strict: false });
  const navigate = useNavigate();
  const tab: Tab = isTab(search.tab) ? search.tab : "trial";

  function select(next: string) {
    if (!isTab(next)) return;
    void navigate({ to: ".", search: next === "trial" ? {} : { tab: next }, replace: true });
  }

  return (
    <>
      <PageHeader title={t("ledger.title")} description={t("ledger.description")} />
      <Tabs value={tab} onValueChange={select}>
        <TabsList aria-label={t("ledger.tabsLabel")}>
          <TabsTrigger value="trial">{t("ledger.tabs.trialBalance")}</TabsTrigger>
          <TabsTrigger value="accounts">{t("ledger.tabs.accounts")}</TabsTrigger>
          <TabsTrigger value="entries">{t("ledger.tabs.entries")}</TabsTrigger>
        </TabsList>
        <TabsContent value="trial">
          <TrialBalanceTab projectId={projectId} />
        </TabsContent>
        <TabsContent value="accounts">
          <AccountsTab projectId={projectId} />
        </TabsContent>
        <TabsContent value="entries">
          <EntriesTab projectId={projectId} />
        </TabsContent>
      </Tabs>
    </>
  );
}

// ---- Trial balance ---------------------------------------------------------------------------------------

function lineBalance(line: TrialBalanceLineOut): bigint {
  const net = BigInt(line.debits) - BigInt(line.credits);
  return line.type === "asset" || line.type === "expense" ? net : -net;
}

function TrialBalanceTab({ projectId }: { projectId: string }) {
  const { t, tLoose } = useI18n();
  const familyName = useFamilyName();
  const query = useTrialBalance(projectId);
  if (query.isPending) return <ListSkeleton label={t("ledger.trial.loading")} />;
  if (query.isError) return <QueryErrorState title={t("ledger.trial.loadFailed")} error={query.error} onRetry={() => void query.refetch()} />;
  const { totals, lines } = query.data;
  if (totals.length === 0 && lines.length === 0) {
    return <EmptyState icon={BookOpenText} title={t("ledger.trial.emptyTitle")} description={t("ledger.trial.emptyDescription")} />;
  }
  const allBalanced = totals.every((total) => total.balanced) && query.data.balanced;
  return (
    <div className="flex flex-col gap-4">
      <p role="status" className={allBalanced ? "text-sm text-ink-muted" : "text-sm text-danger"}>
        {allBalanced ? t("ledger.trial.allBalanced") : t("ledger.trial.someUnbalanced")}
      </p>
      {totals.map((total) => (
        <section
          key={total.currency}
          aria-label={t("ledger.trial.caption", { currency: total.currency })}
          className="rounded-md border border-line bg-surface"
        >
          <header className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-3">
            <h2 className="text-base font-semibold">{t("ledger.trial.caption", { currency: total.currency })}</h2>
            <div className="flex items-center gap-2">
              <span className="text-xs text-ink-muted">{t("ledger.trial.posted")}</span>
              <Badge tone={total.balanced ? "success" : "danger"}>{t(total.balanced ? "ledger.trial.balanced" : "ledger.trial.unbalanced")}</Badge>
            </div>
          </header>
          <Table>
            <caption className="sr-only">{t("ledger.trial.caption", { currency: total.currency })}</caption>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>{t("ledger.trial.cols.account")}</TableHead>
                <TableHead>{t("ledger.trial.cols.type")}</TableHead>
                <TableHead numeric>{t("ledger.trial.cols.debits")}</TableHead>
                <TableHead numeric>{t("ledger.trial.cols.credits")}</TableHead>
                <TableHead numeric>{t("ledger.trial.cols.balance")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {lines
                .filter((line) => line.currency === total.currency)
                .map((line) => (
                  <TableRow key={`${line.type}:${line.code}`}>
                    <TableCell>
                      <div className="flex flex-col gap-0.5">
                        <span className="font-medium">{familyName(line.code)}</span>
                        <span className="font-mono text-xs text-ink-faint">{line.code}</span>
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge tone="neutral">{tLoose("ledger.accountType", line.type, line.type)}</Badge>
                    </TableCell>
                    <TableCell numeric>
                      <Money value={minor(line.debits, line.currency)} />
                    </TableCell>
                    <TableCell numeric>
                      <Money value={minor(line.credits, line.currency)} />
                    </TableCell>
                    <TableCell numeric>
                      <Money value={minor(lineBalance(line), line.currency)} />
                    </TableCell>
                  </TableRow>
                ))}
            </TableBody>
            <tfoot>
              <TableRow className="border-t-2 border-line-strong bg-surface-sunken font-semibold hover:bg-surface-sunken">
                <TableCell colSpan={2}>{t("ledger.trial.total", { currency: total.currency })}</TableCell>
                <TableCell numeric data-testid="trial-debits">
                  <Money value={minor(total.debits, total.currency)} />
                </TableCell>
                <TableCell numeric data-testid="trial-credits">
                  <Money value={minor(total.credits, total.currency)} />
                </TableCell>
                <TableCell />
              </TableRow>
            </tfoot>
          </Table>
        </section>
      ))}
    </div>
  );
}

// ---- Accounts --------------------------------------------------------------------------------------------

function ownerKind(account: AccountOut): "customer" | "payee" | "provider" | "none" {
  if (account.customer_id) return "customer";
  if (account.payee_id) return "payee";
  if (account.provider_account_id) return "provider";
  return "none";
}

function isAccountType(value: string): value is AccountType {
  return ACCOUNT_TYPES.some((type) => type === value);
}

function AccountsTab({ projectId }: { projectId: string }) {
  const { t, tLoose } = useI18n();
  const familyName = useFamilyName();
  const [text, setText] = useState("");
  const [type, setType] = useState<AccountType | "">("");
  const accounts = useAccounts(projectId, type);
  const rows = flattenAccounts(accounts.data);
  const needle = text.trim().toLowerCase();
  const visible = rows.filter((account) => {
    if (needle === "") return true;
    const { family } = splitAccountCode(account.code);
    return `${account.code} ${familyName(family)}`.toLowerCase().includes(needle);
  });
  const filtering = text !== "" || type !== "";
  const clear = () => {
    setText("");
    setType("");
  };

  let body;
  if (accounts.isPending) {
    body = <ListSkeleton label={t("ledger.accounts.loading")} />;
  } else if (accounts.isError) {
    body = <QueryErrorState title={t("ledger.accounts.loadFailed")} error={accounts.error} onRetry={() => void accounts.refetch()} />;
  } else if (rows.length === 0 && type === "") {
    body = <EmptyState icon={BookOpenText} title={t("ledger.accounts.emptyTitle")} description={t("ledger.accounts.emptyDescription")} />;
  } else {
    body = (
      <>
        {visible.length === 0 ? (
          <EmptyState
            icon={Search}
            title={t("ledger.accounts.filters.noMatchTitle")}
            description={t("ledger.accounts.filters.noMatchDescription")}
            action={
              <Button variant="secondary" onClick={clear}>
                {t("ledger.accounts.filters.clear")}
              </Button>
            }
          />
        ) : (
          <div className="rounded-md border border-line bg-surface">
            <Table>
              <caption className="sr-only">{t("ledger.accounts.caption")}</caption>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead>{t("ledger.accounts.cols.code")}</TableHead>
                  <TableHead>{t("ledger.accounts.cols.type")}</TableHead>
                  <TableHead>{t("ledger.accounts.cols.owner")}</TableHead>
                  <TableHead>{t("ledger.accounts.cols.normal")}</TableHead>
                  <TableHead numeric>{t("ledger.accounts.cols.posted")}</TableHead>
                  <TableHead numeric>{t("ledger.accounts.cols.pending")}</TableHead>
                  <TableHead numeric>{t("ledger.accounts.cols.available")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visible.map((account) => (
                  <TableRow key={account.id}>
                    <TableCell>
                      <AccountLabel code={account.code} />
                    </TableCell>
                    <TableCell>
                      <Badge tone="neutral">{tLoose("ledger.accountType", account.type, account.type)}</Badge>
                    </TableCell>
                    <TableCell className="text-ink-muted">{t(`ledger.accounts.owner.${ownerKind(account)}`)}</TableCell>
                    <TableCell>{tLoose("ledger.normal", account.normal_balance, account.normal_balance)}</TableCell>
                    <TableCell numeric>
                      <Money value={minor(account.balance.posted.amount_minor, account.currency)} />
                    </TableCell>
                    <TableCell numeric>
                      <Money value={minor(account.balance.pending.amount_minor, account.currency)} />
                    </TableCell>
                    <TableCell numeric>
                      <Money value={minor(account.balance.available.amount_minor, account.currency)} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-ink-muted" aria-live="polite">
            {t(accounts.hasNextPage ? "ledger.accounts.loadedMore" : "ledger.accounts.loaded", { count: rows.length })}
            {text !== "" ? ` · ${t("ledger.accounts.filters.scopeNote")}` : null}
          </p>
          {accounts.hasNextPage ? (
            <Button variant="secondary" onClick={() => void accounts.fetchNextPage()} disabled={accounts.isFetchingNextPage}>
              {accounts.isFetchingNextPage ? t("ledger.accounts.loadingMore") : t("ledger.accounts.loadMore")}
            </Button>
          ) : null}
        </div>
        {accounts.isFetchNextPageError ? (
          <p role="alert" className="mt-2 text-sm text-danger">
            {t("ledger.accounts.nextPageFailed")}
          </p>
        ) : null}
      </>
    );
  }

  return (
    <>
      <form role="search" aria-label={t("ledger.accounts.filters.label")} onSubmit={(event) => event.preventDefault()} className="mb-4 flex flex-wrap items-end gap-2">
        <div className="min-w-56 flex-1">
          <label htmlFor="account-filter-text" className="mb-1 block text-sm font-medium">
            {t("ledger.accounts.filters.search")}
          </label>
          <Input
            id="account-filter-text"
            value={text}
            onChange={(event) => setText(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            placeholder={t("ledger.accounts.filters.searchPlaceholder")}
          />
        </div>
        <div className="w-full sm:w-48">
          <label htmlFor="account-filter-type" className="mb-1 block text-sm font-medium">
            {t("ledger.accounts.filters.type")}
          </label>
          <Select
            id="account-filter-type"
            value={type}
            onChange={(event) => setType(isAccountType(event.target.value) ? event.target.value : "")}
          >
            <option value="">{t("ledger.accounts.filters.allTypes")}</option>
            {ACCOUNT_TYPES.map((option) => (
              <option key={option} value={option}>
                {t(`ledger.accountType.${option}`)}
              </option>
            ))}
          </Select>
        </div>
        {filtering ? (
          <Button type="button" variant="ghost" onClick={clear}>
            <X aria-hidden="true" />
            {t("ledger.accounts.filters.clear")}
          </Button>
        ) : null}
      </form>
      {body}
    </>
  );
}

// ---- Journal ---------------------------------------------------------------------------------------------

function isEntryStatus(value: string): value is EntryStatus {
  return ENTRY_STATUSES.some((status) => status === value);
}

/** An entry's size: the sum of its debits (equal to its credits), read from the cached entry detail. */
function EntryTotal({ projectId, entryId }: { projectId: string; entryId: string }) {
  const { t } = useI18n();
  const detail = useEntry(projectId, entryId);
  if (!detail.data) return <span className="text-ink-faint" title={t("ledger.entries.totalPending")}>&mdash;</span>;
  const debits = new Map<string, bigint>();
  for (const posting of detail.data.postings) {
    if (posting.direction !== "debit") continue;
    const currency = posting.amount.currency;
    debits.set(currency, (debits.get(currency) ?? 0n) + BigInt(posting.amount.amount_minor));
  }
  const first = [...debits.entries()][0];
  if (!first) return <span className="text-ink-faint">&mdash;</span>;
  return <Money value={minor(first[1], first[0])} />;
}

function EntryRow({ projectId, entry }: { projectId: string; entry: EntryOut }) {
  const { t } = useI18n();
  return (
    <TableRow>
      <TableCell>
        <CopyableId id={entry.id} />
      </TableCell>
      <TableCell>
        <RecipeLabel recipe={entry.recipe} />
      </TableCell>
      <TableCell>
        <EntryStatusBadge status={entry.status} />
      </TableCell>
      <TableCell className="text-ink-muted">
        <DateTime value={entry.effective_at} />
      </TableCell>
      <TableCell numeric>
        <EntryTotal projectId={projectId} entryId={entry.id} />
      </TableCell>
      <TableCell>
        {entry.correlation_id ? (
          <span className="block max-w-[9.5rem] truncate font-mono text-xs text-ink-muted" title={entry.correlation_id}>
            {entry.correlation_id}
          </span>
        ) : (
          <span className="text-ink-faint">&mdash;</span>
        )}
      </TableCell>
      <TableCell className="text-right">
        <Link
          to="/ledger/entries/$entryId"
          params={{ entryId: entry.id }}
          aria-label={t("ledger.entries.openNamed", { id: entry.id })}
          className="rounded-xs text-sm font-medium text-primary underline-offset-2 hover:underline"
        >
          {t("ledger.entries.open")}
        </Link>
      </TableCell>
    </TableRow>
  );
}

function EntriesTab({ projectId }: { projectId: string }) {
  const { t, tLoose } = useI18n();
  const [status, setStatus] = useState<EntryStatus | "">("");
  const [recipe, setRecipe] = useState("");
  const entries = useEntries(projectId, { status, recipe });
  const rows = flattenEntries(entries.data);
  const filtering = status !== "" || recipe !== "";
  const clear = () => {
    setStatus("");
    setRecipe("");
  };

  let body;
  if (entries.isPending) {
    body = <ListSkeleton label={t("ledger.entries.loading")} />;
  } else if (entries.isError) {
    body = <QueryErrorState title={t("ledger.entries.loadFailed")} error={entries.error} onRetry={() => void entries.refetch()} />;
  } else if (rows.length === 0) {
    body = filtering ? (
      <EmptyState
        icon={Search}
        title={t("ledger.entries.filters.noMatchTitle")}
        description={t("ledger.entries.filters.noMatchDescription")}
        action={
          <Button variant="secondary" onClick={clear}>
            {t("ledger.entries.filters.clear")}
          </Button>
        }
      />
    ) : (
      <EmptyState icon={BookOpenText} title={t("ledger.entries.emptyTitle")} description={t("ledger.entries.emptyDescription")} />
    );
  } else {
    body = (
      <>
        <div className="rounded-md border border-line bg-surface">
          <Table>
            <caption className="sr-only">{t("ledger.entries.caption")}</caption>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead>{t("ledger.entries.cols.id")}</TableHead>
                <TableHead>{t("ledger.entries.cols.recipe")}</TableHead>
                <TableHead>{t("ledger.entries.cols.status")}</TableHead>
                <TableHead>{t("ledger.entries.cols.effective")}</TableHead>
                <TableHead numeric>{t("ledger.entries.cols.total")}</TableHead>
                <TableHead>{t("ledger.entries.cols.correlation")}</TableHead>
                <TableHead>
                  <span className="sr-only">{t("ledger.entries.cols.actions")}</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((entry) => (
                <EntryRow key={entry.id} projectId={projectId} entry={entry} />
              ))}
            </TableBody>
          </Table>
        </div>
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-ink-muted" aria-live="polite">
            {t(entries.hasNextPage ? "ledger.entries.loadedMore" : "ledger.entries.loaded", { count: rows.length })}
          </p>
          {entries.hasNextPage ? (
            <Button variant="secondary" onClick={() => void entries.fetchNextPage()} disabled={entries.isFetchingNextPage}>
              {entries.isFetchingNextPage ? t("ledger.entries.loadingMore") : t("ledger.entries.loadMore")}
            </Button>
          ) : null}
        </div>
        {entries.isFetchNextPageError ? (
          <p role="alert" className="mt-2 text-sm text-danger">
            {t("ledger.entries.nextPageFailed")}
          </p>
        ) : null}
      </>
    );
  }

  return (
    <>
      <form role="search" aria-label={t("ledger.entries.filters.label")} onSubmit={(event) => event.preventDefault()} className="mb-4 flex flex-wrap items-end gap-2">
        <div className="w-full sm:w-56">
          <label htmlFor="entry-filter-recipe" className="mb-1 block text-sm font-medium">
            {t("ledger.entries.filters.recipe")}
          </label>
          <Select id="entry-filter-recipe" value={recipe} onChange={(event) => setRecipe(event.target.value)}>
            <option value="">{t("ledger.entries.filters.allRecipes")}</option>
            {FILTER_RECIPES.map((code) => (
              <option key={code} value={code}>
                {code} · {tLoose("ledger.recipe", code, code)}
              </option>
            ))}
          </Select>
        </div>
        <div className="w-full sm:w-48">
          <label htmlFor="entry-filter-status" className="mb-1 block text-sm font-medium">
            {t("ledger.entries.filters.status")}
          </label>
          <Select
            id="entry-filter-status"
            value={status}
            onChange={(event) => setStatus(isEntryStatus(event.target.value) ? event.target.value : "")}
          >
            <option value="">{t("ledger.entries.filters.allStatuses")}</option>
            {ENTRY_STATUSES.map((option) => (
              <option key={option} value={option}>
                {t(`ledger.status.${option}`)}
              </option>
            ))}
          </Select>
        </div>
        {filtering ? (
          <Button type="button" variant="ghost" onClick={clear}>
            <X aria-hidden="true" />
            {t("ledger.entries.filters.clear")}
          </Button>
        ) : null}
      </form>
      {body}
    </>
  );
}
