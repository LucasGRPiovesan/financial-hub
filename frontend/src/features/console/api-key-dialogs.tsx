import { Check, Copy } from "lucide-react";
import { useState, type FormEvent } from "react";

import { Field } from "../../components/form-field";
import { Button } from "../../components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { useToast } from "../../components/ui/use-toast";
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
      { onError: () => toast({ tone: "danger", title: "Couldn't create the API key" }) },
    );
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
      <DialogHeader>
        <DialogTitle>Create API key</DialogTitle>
        <DialogDescription>The key is shown once, right after you create it.</DialogDescription>
      </DialogHeader>
      <Field label="Name" error={fields.name}>
        <Input value={name} onChange={(event) => setName(event.target.value)} autoComplete="off" required />
      </Field>
      <fieldset className="flex flex-col gap-2" aria-describedby={scopeError ? "api-key-scopes-error" : undefined}>
        <legend className="mb-1 text-sm font-medium text-ink">Scopes</legend>
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
              <span className="block text-xs text-ink-muted">{scope.description}</span>
            </span>
          </label>
        ))}
        {scopeError ? (
          <p id="api-key-scopes-error" className="text-sm text-danger">
            {scopeError}
          </p>
        ) : null}
      </fieldset>
      <Field label="Expires (optional)" error={fields.expires_at} hint="Leave empty for a key that doesn't expire.">
        <Input type="datetime-local" value={expiresAt} onChange={(event) => setExpiresAt(event.target.value)} />
      </Field>
      {form ? (
        <p role="alert" className="text-sm text-danger">
          {form}
        </p>
      ) : null}
      <DialogFooter>
        <Button type="button" variant="secondary" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="submit" disabled={create.isPending}>
          {create.isPending ? "Creating…" : "Create key"}
        </Button>
      </DialogFooter>
    </form>
  );
}

function IssuedKeyPanel({ plaintext, warn, onStored }: { plaintext: string; warn: boolean; onStored: () => void }) {
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
        <DialogTitle>Your new API key</DialogTitle>
        <DialogDescription>
          Copy it now and keep it somewhere safe. For security it can't be shown again.
        </DialogDescription>
      </DialogHeader>
      <div className="flex gap-2">
        <Input readOnly aria-label="API key" value={plaintext} className="font-mono text-xs" onFocus={(e) => e.currentTarget.select()} />
        <Button type="button" variant="secondary" onClick={() => void copy()}>
          {copied === "yes" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
          {copied === "yes" ? "Copied" : "Copy"}
        </Button>
      </div>
      {copied === "failed" ? (
        <p role="status" className="text-sm text-ink-muted">
          Couldn't access the clipboard. Select the key above and copy it manually.
        </p>
      ) : null}
      {warn ? (
        <p role="alert" className="text-sm text-danger">
          Copy the key and confirm before closing — it can't be shown again.
        </p>
      ) : null}
      <DialogFooter>
        <Button type="button" onClick={onStored}>
          I stored it
        </Button>
      </DialogFooter>
    </div>
  );
}
