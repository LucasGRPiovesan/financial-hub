import { KeyRound, TriangleAlert } from "lucide-react";
import { useState, type FormEvent } from "react";

import type { CatalogEntryOut, ProviderAccountOut } from "../../api/generated/model";
import { Field } from "../../components/form-field";
import { Button } from "../../components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { useToast } from "../../components/ui/use-toast";
import { camelKey, useI18n } from "../../i18n";
import { useReplaceProviderCredentials } from "../console/api";
import { fieldErrorsOf } from "../console/errors";
import { CopyButton } from "../events/event-parts";
import { WEBHOOK_TOKEN_FIELD, asaasWebhookUrl, generateWebhookToken } from "./webhook-credentials";

function labelOf(field: string): string {
  const text = field.replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function definedCredentials(values: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(values).filter(([, value]) => value !== ""));
}

/**
 * Replace a provider account's credentials. Saving replaces the whole set, so the form says so up front.
 *
 * Secret inputs start empty and are never filled from the server. The Asaas webhook token is generated here
 * (the backend only stores it), shown so it can be copied into the Asaas panel, and sent with the credentials.
 */
export function CredentialsDialog({
  projectId,
  account,
  entry,
  onClose,
}: {
  projectId: string;
  account: ProviderAccountOut | null;
  entry: CatalogEntryOut | undefined;
  onClose: () => void;
}) {
  return (
    <Dialog open={account !== null} onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto">
        {account ? <CredentialsForm projectId={projectId} account={account} entry={entry} onDone={onClose} /> : null}
      </DialogContent>
    </Dialog>
  );
}

function CredentialsForm({
  projectId,
  account,
  entry,
  onDone,
}: {
  projectId: string;
  account: ProviderAccountOut;
  entry: CatalogEntryOut | undefined;
  onDone: () => void;
}) {
  const toast = useToast();
  const { t, tLoose } = useI18n();
  const replace = useReplaceProviderCredentials(projectId);
  const [values, setValues] = useState<Record<string, string>>({});
  const { fields, form } = fieldErrorsOf(replace.error);

  const required = entry?.credential_fields ?? [];
  const optional = entry?.optional_credential_fields ?? [];
  const hasWebhookToken = [...required, ...optional].includes(WEBHOOK_TOKEN_FIELD);
  const nameOf = (field: string) => tLoose("providers.credentialFields", camelKey(field), labelOf(field));
  const setValue = (field: string, value: string) => setValues((current) => ({ ...current, [field]: value }));

  function submit(event: FormEvent) {
    event.preventDefault();
    replace.mutate(
      { accountId: account.id, credentials: definedCredentials(values) },
      {
        onSuccess: () => {
          toast({ tone: "success", title: t("providers.credentialsUpdated"), description: account.name });
          onDone();
        },
        onError: () => toast({ tone: "danger", title: t("providers.credentialsFailed") }),
      },
    );
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
      <DialogHeader>
        <DialogTitle>{t("providers.replaceTitle")}</DialogTitle>
        <DialogDescription>{t("providers.replaceDescription", { name: account.name })}</DialogDescription>
      </DialogHeader>

      <p className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning-soft px-3 py-2 text-sm text-ink" data-testid="replace-warning">
        <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden="true" />
        <span>{t("providers.credentials.replaceWarning")}</span>
      </p>

      {[...required.map((field) => [field, true] as const), ...optional.map((field) => [field, false] as const)].map(([field, isRequired]) => {
        const label = isRequired ? nameOf(field) : t("providers.optionalField", { field: nameOf(field) });
        if (field === WEBHOOK_TOKEN_FIELD) {
          return (
            <div key={field} className="flex flex-col gap-2">
              <Field label={label} error={fields[`credentials.${field}`]} hint={t("providers.credentials.tokenHint")}>
                <Input
                  className="font-mono text-xs"
                  autoComplete="off"
                  spellCheck={false}
                  value={values[field] ?? ""}
                  onChange={(event) => setValue(field, event.target.value)}
                  required={isRequired}
                />
              </Field>
              <div className="flex flex-wrap items-center gap-2">
                <Button type="button" variant="secondary" size="sm" onClick={() => setValue(field, generateWebhookToken())}>
                  <KeyRound aria-hidden="true" />
                  {values[field] ? t("providers.credentials.tokenRegenerate") : t("providers.credentials.tokenGenerate")}
                </Button>
                {values[field] ? <CopyButton text={values[field]} label={t("providers.credentials.tokenCopy")} /> : null}
              </div>
            </div>
          );
        }
        return (
          <Field key={field} label={label} error={fields[`credentials.${field}`]}>
            <Input type="password" autoComplete="new-password" value={values[field] ?? ""} onChange={(event) => setValue(field, event.target.value)} required={isRequired} />
          </Field>
        );
      })}

      {hasWebhookToken ? (
        <div className="flex flex-col gap-2 rounded-md border border-line bg-surface-sunken p-3">
          <Field label={t("providers.credentials.urlLabel")} hint={t("providers.credentials.urlHint")}>
            <Input readOnly className="font-mono text-xs" value={asaasWebhookUrl(account.id)} onFocus={(event) => event.currentTarget.select()} />
          </Field>
          <div>
            <CopyButton text={asaasWebhookUrl(account.id)} label={t("providers.credentials.copyUrl")} />
          </div>
          <h3 className="mt-1 text-sm font-medium">{t("providers.credentials.instructionsTitle")}</h3>
          <ol className="list-decimal pl-5 text-xs text-ink-muted">
            <li>{t("providers.credentials.instructionsUrl")}</li>
            <li>{t("providers.credentials.instructionsToken")}</li>
          </ol>
        </div>
      ) : null}

      {form ? (
        <p role="alert" className="text-sm text-danger">
          {form}
        </p>
      ) : null}
      <DialogFooter>
        <Button type="button" variant="secondary" onClick={onDone}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" disabled={replace.isPending}>
          {replace.isPending ? t("providers.saving") : t("providers.replaceLabel")}
        </Button>
      </DialogFooter>
    </form>
  );
}
