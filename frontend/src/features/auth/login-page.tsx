import { useQueryClient } from "@tanstack/react-query";
import { useMutation } from "@tanstack/react-query";
import { useRouter } from "@tanstack/react-router";
import { CircleAlert, LogIn } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";

import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { Label } from "../../components/ui/label";
import { isApiError } from "../../lib/api";
import { BrandMark } from "../shell/sidebar-nav";
import { login, meQueryOptions } from "./auth-api";
import { safeRedirectTarget } from "./redirect";

type FailureKind = "credentials" | "rate-limit" | "network" | "other";

interface Failure {
  kind: FailureKind;
  message: string;
}

function describeFailure(error: unknown): { failure: Failure; retryAfter: number | null } {
  if (!isApiError(error)) {
    return { failure: { kind: "other", message: "Something went wrong. Try again." }, retryAfter: null };
  }
  if (error.isNetworkError) {
    return {
      failure: { kind: "network", message: "Can't reach the Financial Hub API. Check your connection and try again." },
      retryAfter: null,
    };
  }
  if (error.status === 401) {
    return { failure: { kind: "credentials", message: "The email or password is incorrect." }, retryAfter: null };
  }
  if (error.status === 429) {
    return {
      failure: {
        kind: "rate-limit",
        message: "Too many sign-in attempts. Wait before trying again.",
      },
      retryAfter: error.retryAfterSeconds ?? 30,
    };
  }
  return { failure: { kind: "other", message: error.detail ?? error.title }, retryAfter: null };
}

export function LoginPage({ redirectTo }: { redirectTo?: string }) {
  const queryClient = useQueryClient();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [failure, setFailure] = useState<Failure | null>(null);
  const [waitSeconds, setWaitSeconds] = useState(0);

  useEffect(() => {
    if (waitSeconds <= 0) return;
    const timer = window.setTimeout(() => setWaitSeconds((seconds) => seconds - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [waitSeconds]);

  const signIn = useMutation({
    mutationFn: login,
    onSuccess: async () => {
      queryClient.removeQueries({ queryKey: meQueryOptions.queryKey });
      router.history.push(safeRedirectTarget(redirectTo) ?? "/");
    },
    onError: (error) => {
      const { failure: next, retryAfter } = describeFailure(error);
      setFailure(next);
      setWaitSeconds(retryAfter ?? 0);
    },
  });

  const locked = waitSeconds > 0;
  const invalid = failure?.kind === "credentials";

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (locked || signIn.isPending) return;
    setFailure(null);
    signIn.mutate({ email: email.trim(), password });
  }

  return (
    <div className="grid min-h-dvh lg:grid-cols-[minmax(0,5fr)_minmax(0,4fr)]">
      <aside
        aria-hidden="true"
        className="relative hidden overflow-hidden bg-chrome p-12 lg:flex lg:flex-col lg:justify-between"
        style={{
          backgroundImage:
            "repeating-linear-gradient(to bottom, transparent 0, transparent 35px, var(--color-chrome-line) 35px, var(--color-chrome-line) 36px)",
        }}
      >
        <BrandMark />
        <div className="max-w-md">
          <p className="text-2xl leading-tight font-semibold tracking-tight text-chrome-ink">
            Every movement of money, accounted for twice.
          </p>
          <p className="mt-3 text-sm text-chrome-muted">
            The operator console for Financial Hub: ledger, payments, billing and reconciliation in one place.
          </p>
        </div>
        <div className="absolute inset-y-0 left-12 w-px bg-chrome-brass/50" />
      </aside>

      <main className="flex items-center justify-center bg-canvas px-5 py-12">
        <div className="w-full max-w-sm">
          <BrandMark className="mb-8 lg:hidden [&>span:last-child]:text-ink" />
          <h1 className="text-2xl font-semibold tracking-tight">Sign in</h1>
          <p className="mt-1 text-sm text-ink-muted">Use your operator account to open the console.</p>

          <form onSubmit={onSubmit} className="mt-6 flex flex-col gap-4" aria-describedby={failure ? "login-error" : undefined}>
            <div aria-live="polite" id="login-error">
              {failure ? (
                <div
                  role="alert"
                  className="flex items-start gap-2 rounded-sm border border-danger bg-danger-soft p-3 text-sm text-danger"
                >
                  <CircleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                  <p>
                    {failure.message}
                    {failure.kind === "rate-limit" && locked ? ` Try again in ${waitSeconds}s.` : null}
                  </p>
                </div>
              ) : null}
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="login-email">Email</Label>
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
              <Label htmlFor="login-password">Password</Label>
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
              {signIn.isPending ? "Signing in…" : locked ? `Try again in ${waitSeconds}s` : "Sign in"}
            </Button>
          </form>
        </div>
      </main>
    </div>
  );
}
