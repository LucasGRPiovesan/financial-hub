import { Check, Copy } from "lucide-react";
import { useState, type FormEvent } from "react";

import { Field } from "../../components/form-field";
import { Button } from "../../components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { useToast } from "../../components/ui/use-toast";
import { useT } from "../../i18n";
import { useCreateApiKey } from "./api";
import { GRANTABLE_API_KEY_SCOPES } from "./permissions";
import { fieldErrorsOf } from "./errors";

interface Props {
  projectId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Create an API key, then show its plaintext exactly once.
 *
 * The plaintext lives only in this component's state (`issued`). It never enters the mutation result or the
 * query cache (see `useCreateApiKey`), and it is dropped as soon as the operator confirms they stored it.
 * Until then the dialog cannot be dismissed by Escape, an outside click or the X.
 */
export function CreateApiKeyDialog({ projectId, open, onOpenChange }: Props) {
  const [issued, setIssued] = useState<string | null>(null);
  const [warn, setWarn] = useState(false);

  function handleOpenChange(next: boolean) {
    if (!next && issued !== null) {
      setWarn(true);
      return;
    }
    if (!next) setWarn(false);
    onOpenChange(next);
  }

  function finish() {
    setIssued(null);
    setWarn(false);
    onOpenChange(false);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent>
        {issued === null ? (
          <CreateApiKeyForm projectId={projectId} onCancel={finish} onIssued={setIssued} />
        ) : (
          <IssuedKeyPanel plaintext={issued} warn={warn} onStored={finish} />
        )}
      </DialogContent>
    </Dialog>
  );
}

function CreateApiKeyForm({
  projectId,
  onCancel,
  onIssued,
}: {
  projectId: string;
  onCancel: () => void;
  onIssued: (plaintext: string) => void;
}) {
  const toast = useToast();
  const t = useT();
  const create = useCreateApiKey(projectId);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>([]);
  const [expiresAt, setExpiresAt] = useState("");
  const { fields, form } = fieldErrorsOf(create.error);
  const scopeError = fields.scopes ?? Object.entries(fields).find(([key]) => key.startsWith("scopes."))?.[1];

  function toggle(scope: string, checked: boolean) {
    setScopes((current) => (checked ? [...current, scope] : current.filter((item) => item !== scope)));
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const expiry = expiresAt ? new Date(expiresAt) : null;
    create.mutate(
      {
        request: {
          name: name.trim(),
          scopes,
          expires_at: expiry && !Number.isNaN(expiry.getTime()) ? expiry.toISOString() : null,
        },
        onKey: onIssued,
      },
      { onError: () => toast({ tone: "danger", title: t("apiKeys.createFailed") }) },
    );
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
      <DialogHeader>
        <DialogTitle>{t("apiKeys.create")}</DialogTitle>
        <DialogDescription>{t("apiKeys.createDescription")}</DialogDescription>
      </DialogHeader>
      <Field label={t("apiKeys.nameLabel")} error={fields.name}>
        <Input value={name} onChange={(event) => setName(event.target.value)} autoComplete="off" required />
      </Field>
      <fieldset className="flex flex-col gap-2" aria-describedby={scopeError ? "api-key-scopes-error" : undefined}>
        <legend className="mb-1 text-sm font-medium text-ink">{t("apiKeys.scopesLegend")}</legend>
        {GRANTABLE_API_KEY_SCOPES.map((scope) => (
          <label key={scope.value} className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              className="mt-1"
              checked={scopes.includes(scope.value)}
              onChange={(event) => toggle(scope.value, event.target.checked)}
            />
            <span>
              <span className="font-mono text-xs">{scope.value}</span>
              <span className="block text-xs text-ink-muted">{t(scope.descriptionKey)}</span>
            </span>
          </label>
        ))}
        {scopeError ? (
          <p id="api-key-scopes-error" className="text-sm text-danger">
            {scopeError}
          </p>
        ) : null}
      </fieldset>
      <Field label={t("apiKeys.expiresLabel")} error={fields.expires_at} hint={t("apiKeys.expiresHint")}>
        <Input type="datetime-local" value={expiresAt} onChange={(event) => setExpiresAt(event.target.value)} />
      </Field>
      {form ? (
        <p role="alert" className="text-sm text-danger">
          {form}
        </p>
      ) : null}
      <DialogFooter>
        <Button type="button" variant="secondary" onClick={onCancel}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" disabled={create.isPending}>
          {create.isPending ? t("apiKeys.creating") : t("apiKeys.createKey")}
        </Button>
      </DialogFooter>
    </form>
  );
}

function IssuedKeyPanel({ plaintext, warn, onStored }: { plaintext: string; warn: boolean; onStored: () => void }) {
  const t = useT();
  const [copied, setCopied] = useState<"yes" | "failed" | null>(null);

  async function copy() {
    try {
      await navigator.clipboard.writeText(plaintext);
      setCopied("yes");
    } catch {
      setCopied("failed");
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <DialogHeader>
        <DialogTitle>{t("apiKeys.issuedTitle")}</DialogTitle>
        <DialogDescription>{t("apiKeys.issuedDescription")}</DialogDescription>
      </DialogHeader>
      <div className="flex gap-2">
        <Input readOnly aria-label={t("apiKeys.keyLabel")} value={plaintext} className="font-mono text-xs" onFocus={(e) => e.currentTarget.select()} />
        <Button type="button" variant="secondary" onClick={() => void copy()}>
          {copied === "yes" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
          {copied === "yes" ? t("apiKeys.copied") : t("apiKeys.copy")}
        </Button>
      </div>
      {copied === "failed" ? (
        <p role="status" className="text-sm text-ink-muted">
          {t("apiKeys.clipboardFailed")}
        </p>
      ) : null}
      {warn ? (
        <p role="alert" className="text-sm text-danger">
          {t("apiKeys.warn")}
        </p>
      ) : null}
      <DialogFooter>
        <Button type="button" onClick={onStored}>
          {t("apiKeys.stored")}
        </Button>
      </DialogFooter>
    </div>
  );
}
