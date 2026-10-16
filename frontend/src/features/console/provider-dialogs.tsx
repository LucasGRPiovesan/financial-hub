import { useState, type FormEvent } from "react";

import type { CatalogEntryOut, ProviderAccountOut } from "../../api/generated/model";
import { Field } from "../../components/form-field";
import { Button } from "../../components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { useToast } from "../../components/ui/use-toast";
import { camelKey, useI18n } from "../../i18n";
import { useCreateProviderAccount, useReplaceProviderCredentials } from "./api";
import { fieldErrorsOf } from "./errors";

const SELECT_CLASS =
  "h-9 w-full rounded-md border border-line bg-surface px-3 text-sm text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary";

function labelOf(field: string): string {
  const text = field.replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Secret inputs: always empty on open, never echoed back, and not offered to password managers. */
function CredentialInputs({
  required,
  optional,
  values,
  onChange,
  errors,
}: {
  required: readonly string[];
  optional: readonly string[];
  values: Record<string, string>;
  onChange: (field: string, value: string) => void;
  errors: Record<string, string>;
}) {
  const { t, tLoose } = useI18n();
  const nameOf = (field: string) => tLoose("providers.credentialFields", camelKey(field), labelOf(field));
  return (
    <>
      {[...required.map((field) => [field, true] as const), ...optional.map((field) => [field, false] as const)].map(
        ([field, isRequired]) => (
          <Field key={field} label={isRequired ? nameOf(field) : t("providers.optionalField", { field: nameOf(field) })} error={errors[`credentials.${field}`]}>
            <Input
              type="password"
              autoComplete="new-password"
              value={values[field] ?? ""}
              onChange={(event) => onChange(field, event.target.value)}
              required={isRequired}
            />
          </Field>
        ),
      )}
    </>
  );
}

function definedCredentials(values: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(values).filter(([, value]) => value !== ""));
}

export function CreateProviderAccountDialog({
  projectId,
  catalog,
  open,
  onOpenChange,
}: {
  projectId: string;
  catalog: readonly CatalogEntryOut[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <CreateForm projectId={projectId} catalog={catalog} onDone={() => onOpenChange(false)} />
      </DialogContent>
    </Dialog>
  );
}

function CreateForm({ projectId, catalog, onDone }: { projectId: string; catalog: readonly CatalogEntryOut[]; onDone: () => void }) {
  const toast = useToast();
  const { t } = useI18n();
  const create = useCreateProviderAccount(projectId);
  const enabled = catalog.filter((entry) => entry.enabled);
  const [provider, setProvider] = useState(enabled[0]?.provider ?? "");
  const entry = enabled.find((item) => item.provider === provider);
  const [environment, setEnvironment] = useState("");
  const [name, setName] = useState("");
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const { fields, form } = fieldErrorsOf(create.error);
  const chosenEnvironment = environment || entry?.environments[0] || "";

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate(
      { provider, environment: chosenEnvironment, name: name.trim(), credentials: definedCredentials(credentials) },
      {
        onSuccess: () => {
          toast({ tone: "success", title: t("providers.created"), description: name.trim() });
          onDone();
        },
        onError: () => toast({ tone: "danger", title: t("providers.createFailed") }),
      },
    );
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
      <DialogHeader>
        <DialogTitle>{t("providers.createTitle")}</DialogTitle>
        <DialogDescription>{t("providers.createDescription")}</DialogDescription>
      </DialogHeader>
      <Field label={t("providers.providerLabel")} error={fields.provider}>
        <select
          className={SELECT_CLASS}
          value={provider}
          onChange={(event) => {
            setProvider(event.target.value);
            setEnvironment("");
            setCredentials({});
          }}
        >
          {enabled.map((item) => (
            <option key={item.provider} value={item.provider}>
              {item.provider}
            </option>
          ))}
        </select>
      </Field>
      <Field label={t("providers.environmentLabel")} error={fields.environment}>
        <select className={SELECT_CLASS} value={chosenEnvironment} onChange={(event) => setEnvironment(event.target.value)}>
          {(entry?.environments ?? []).map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
      </Field>
      <Field label={t("providers.nameLabel")} error={fields.name}>
        <Input value={name} onChange={(event) => setName(event.target.value)} autoComplete="off" required />
      </Field>
      <CredentialInputs
        required={entry?.credential_fields ?? []}
        optional={entry?.optional_credential_fields ?? []}
        values={credentials}
        onChange={(field, value) => setCredentials((current) => ({ ...current, [field]: value }))}
        errors={fields}
      />
      {form ? (
        <p role="alert" className="text-sm text-danger">
          {form}
        </p>
      ) : null}
      <DialogFooter>
        <Button type="button" variant="secondary" onClick={onDone}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" disabled={create.isPending || !entry}>
          {create.isPending ? t("providers.creating") : t("providers.addAccount")}
        </Button>
      </DialogFooter>
    </form>
  );
}

export function ReplaceCredentialsDialog({
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
      <DialogContent>
        {account ? <ReplaceForm projectId={projectId} account={account} entry={entry} onDone={onClose} /> : null}
      </DialogContent>
    </Dialog>
  );
}

function ReplaceForm({
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
  const { t } = useI18n();
  const replace = useReplaceProviderCredentials(projectId);
  const [credentials, setCredentials] = useState<Record<string, string>>({});
  const { fields, form } = fieldErrorsOf(replace.error);

  function submit(event: FormEvent) {
    event.preventDefault();
    replace.mutate(
      { accountId: account.id, credentials: definedCredentials(credentials) },
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
      <CredentialInputs
        required={entry?.credential_fields ?? []}
        optional={entry?.optional_credential_fields ?? []}
        values={credentials}
        onChange={(field, value) => setCredentials((current) => ({ ...current, [field]: value }))}
        errors={fields}
      />
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
