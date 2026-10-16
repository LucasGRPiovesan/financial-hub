import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "@tanstack/react-router";
import { CircleAlert, CreditCard, LogIn, Scale, ScrollText, Webhook } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";

import type { DemoOperatorOut } from "../../api/generated/model";
import { LanguageMenu } from "../../components/language-menu";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { Label } from "../../components/ui/label";
import { Skeleton } from "../../components/ui/skeleton";
import { t, useI18n, type MessageKey } from "../../i18n";
import { isApiError } from "../../lib/api";
import { errorText } from "../console/errors";
import { useDocumentTitle } from "../shell/document-title";
import { BrandMark } from "../shell/sidebar-nav";
import { demoLogin, demoOperatorsQueryOptions, login, meQueryOptions } from "./auth-api";
import { DemoPicker } from "./demo-picker";
import { safeRedirectTarget } from "./redirect";

const CAPABILITIES: readonly { icon: typeof Scale; titleKey: MessageKey; textKey: MessageKey }[] = [
  { icon: Scale, titleKey: "auth.capabilities.ledger.title", textKey: "auth.capabilities.ledger.text" },
  { icon: CreditCard, titleKey: "auth.capabilities.payments.title", textKey: "auth.capabilities.payments.text" },
  { icon: Webhook, titleKey: "auth.capabilities.webhooks.title", textKey: "auth.capabilities.webhooks.text" },
  { icon: ScrollText, titleKey: "auth.capabilities.audit.title", textKey: "auth.capabilities.audit.text" },
];

type FailureKind = "credentials" | "rate-limit" | "network" | "other";

interface Failure {
  kind: FailureKind;
  message: string;
}

function describeFailure(error: unknown): { failure: Failure; retryAfter: number | null } {
  if (!isApiError(error)) {
    return { failure: { kind: "other", message: t("auth.failureOther") }, retryAfter: null };
  }
  if (error.isNetworkError) {
    return {
      failure: { kind: "network", message: t("auth.failureNetwork") },
      retryAfter: null,
    };
  }
  if (error.status === 401) {
    return { failure: { kind: "credentials", message: t("auth.failureCredentials") }, retryAfter: null };
  }
  if (error.status === 429) {
    return {
      failure: {
        kind: "rate-limit",
        message: t("auth.failureRateLimit"),
      },
      retryAfter: error.retryAfterSeconds ?? 30,
    };
  }
  return { failure: { kind: "other", message: errorText(error) }, retryAfter: null };
}

/** Decorative: a posted entry that balances, the product's central promise. Hidden from assistive tech and on short screens. */
function LedgerSample() {
  const { t } = useI18n();
  return (
    <div
      aria-hidden="true"
      className="hidden max-w-lg rounded-md border border-chrome-line bg-chrome-raised/60 p-4 font-mono text-xs text-chrome-muted [@media(min-height:800px)]:block"
    >
      <div className="flex items-center justify-between pb-2 text-chrome-ink">
        <span>{t("auth.sample.entry")}</span>
        <span className="rounded-xs bg-chrome-brass/15 px-1.5 text-chrome-brass">{t("auth.sample.balanced")}</span>
      </div>
      <div className="grid grid-cols-[1fr_auto_auto] gap-x-6 gap-y-1 border-t border-chrome-line pt-2">
        <span>{t("auth.sample.receivables")}</span>
        <span className="text-right text-chrome-ink">R$ 1.250,00</span>
        <span />
        <span className="pl-4">{t("auth.sample.revenue")}</span>
        <span />
        <span className="text-right text-chrome-ink">R$ 1.250,00</span>
      </div>
    </div>
  );
}

export function LoginPage({ redirectTo }: { redirectTo?: string }) {
  const { t } = useI18n();
  useDocumentTitle(t("auth.signIn"));
  const queryClient = useQueryClient();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [failure, setFailure] = useState<Failure | null>(null);
  const [waitSeconds, setWaitSeconds] = useState(0);
  const [usePassword, setUsePassword] = useState(false);
  const [pendingOperator, setPendingOperator] = useState<string | null>(null);
  const demo = useQuery(demoOperatorsQueryOptions);
  const demoOperators = demo.data ?? [];
  const locked = waitSeconds > 0;
  const showPicker = !usePassword && demoOperators.length > 0;
  const checkingDemo = demo.isPending;

  useEffect(() => {
    if (waitSeconds <= 0) return;
    const timer = window.setTimeout(() => setWaitSeconds((seconds) => seconds - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [waitSeconds]);

  // Password and demo sign-in end the same way: forget the cached "signed out" probe and go where the operator was headed.
  function enter() {
    queryClient.removeQueries({ queryKey: meQueryOptions.queryKey });
    router.history.push(safeRedirectTarget(redirectTo) ?? "/");
  }
  function fail(error: unknown) {
    const { failure: next, retryAfter } = describeFailure(error);
    setFailure(next);
    setWaitSeconds(retryAfter ?? 0);
  }

  const signIn = useMutation({ mutationFn: login, onSuccess: enter, onError: fail });
  const demoSignIn = useMutation({
    mutationFn: (operator: DemoOperatorOut) => demoLogin(operator.id),
    onSuccess: enter,
    onError: (error) => {
      if (isApiError(error) && (error.status === 404 || error.status === 401)) {
        // Demo login was switched off (or the persona is gone) since the page loaded: fall back to the password form.
        void queryClient.invalidateQueries({ queryKey: demoOperatorsQueryOptions.queryKey });
        setFailure({ kind: "other", message: t("auth.demoUnavailable") });
        setUsePassword(true);
        return;
      }
      fail(error);
    },
    onSettled: () => setPendingOperator(null),
  });

  function pick(operator: DemoOperatorOut) {
    if (locked || demoSignIn.isPending) return;
    setFailure(null);
    setPendingOperator(operator.id);
    demoSignIn.mutate(operator);
  }

  const invalid = failure?.kind === "credentials";

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (locked || signIn.isPending) return;
    setFailure(null);
    signIn.mutate({ email: email.trim(), password });
  }

  return (
    <div className="grid min-h-dvh lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
      <aside
        aria-label={t("auth.aboutLabel")}
        className="relative hidden overflow-hidden bg-chrome px-10 py-9 xl:px-14 lg:flex lg:flex-col lg:justify-between"
      >
        <BrandMark />
        <div className="max-w-lg">
          <h2 className="text-xl leading-tight font-semibold tracking-tight text-chrome-ink">
            {t("auth.tagline")}
          </h2>
          <p className="mt-3 text-sm text-chrome-muted">{t("auth.intro")}</p>
          <ul className="mt-7 flex flex-col gap-4">
            {CAPABILITIES.map(({ icon: Icon, titleKey, textKey }) => (
              <li key={titleKey} className="flex items-start gap-3">
                <Icon className="mt-0.5 size-4 shrink-0 text-chrome-brass" aria-hidden="true" />
                <div>
                  <p className="text-sm font-medium text-chrome-ink">{t(titleKey)}</p>
                  <p className="text-xs text-chrome-muted">{t(textKey)}</p>
                </div>
              </li>
            ))}
          </ul>
        </div>
        <LedgerSample />
        <p className="border-t border-chrome-line pt-4 text-xs text-chrome-muted">
          {t("auth.evalNotice")}
        </p>
      </aside>

      <main className="flex items-center justify-center bg-canvas px-5 py-8 sm:px-8">
        <div className="w-full max-w-xl">
          <div className="mb-6 flex justify-end">
            <LanguageMenu />
          </div>
          <BrandMark className="mb-8 lg:hidden [&>span:last-child]:text-ink" />
          <h1 className="text-2xl font-semibold tracking-tight">{t("auth.signIn")}</h1>
          <p className="mt-1 text-sm text-ink-muted">
            {showPicker ? t("auth.pickPrompt") : t("auth.accountPrompt")}
          </p>

          <div aria-live="polite" id="login-error" className="mt-4 empty:hidden">
            {failure ? (
              <div
                role="alert"
                className="flex items-start gap-2 rounded-sm border border-danger bg-danger-soft p-3 text-sm text-danger"
              >
                <CircleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                <p>
                  {failure.message}
                  {failure.kind === "rate-limit" && locked ? ` ${t("auth.retryInSentence", { seconds: waitSeconds })}` : null}
                </p>
              </div>
            ) : null}
          </div>

          {checkingDemo ? (
            <div role="status" aria-label={t("auth.checking")} className="mt-6 flex flex-col gap-3">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-2/3" />
            </div>
          ) : showPicker ? (
            <>
              <DemoPicker
                operators={demoOperators}
                pendingId={pendingOperator}
                disabled={demoSignIn.isPending || locked}
                onPick={pick}
              />
              <Button
                variant="link"
                size="sm"
                className="mt-4 px-0"
                onClick={() => {
                  setFailure(null);
                  setUsePassword(true);
                }}
              >
                {t("auth.usePassword")}
              </Button>
            </>
          ) : (
            <>
              <form onSubmit={onSubmit} className="mt-6 flex flex-col gap-4" aria-describedby={failure ? "login-error" : undefined}>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="login-email">{t("auth.email")}</Label>
                  <Input
                    id="login-email"
                    name="email"
                    type="email"
                    autoComplete="username"
                    required
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    aria-invalid={invalid || undefined}
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="login-password">{t("auth.password")}</Label>
                  <Input
                    id="login-password"
                    name="password"
                    type="password"
                    autoComplete="current-password"
                    required
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    aria-invalid={invalid || undefined}
                  />
                </div>

                <Button type="submit" size="lg" disabled={signIn.isPending || locked} className="mt-1">
                  <LogIn aria-hidden="true" />
                  {signIn.isPending ? t("auth.signingIn") : locked ? t("auth.retryIn", { seconds: waitSeconds }) : t("auth.signIn")}
                </Button>
              </form>
              {demoOperators.length > 0 ? (
                <Button
                  variant="link"
                  size="sm"
                  className="mt-4 px-0"
                  onClick={() => {
                    setFailure(null);
                    setUsePassword(false);
                  }}
                >
                  {t("auth.backToDemo")}
                </Button>
              ) : null}
            </>
          )}
        </div>
      </main>
    </div>
  );
}
