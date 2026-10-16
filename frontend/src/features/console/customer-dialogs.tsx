import { useState, type FormEvent } from "react";

import type { CustomerOut, UpsertCustomerRequest } from "../../api/generated/model";
import { Field } from "../../components/form-field";
import { Button } from "../../components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Select } from "../../components/ui/select";
import { Textarea } from "../../components/ui/textarea";
import { useToast } from "../../components/ui/use-toast";
import { t, useT } from "../../i18n";
import { documentError, externalRefError, type CustomerKind } from "../../lib/br-document";
import { isKind, kindLabel } from "./customer-kinds";
import { useAnonymizeCustomer, useUpsertCustomer } from "./customers-api";
import { fieldErrorsOf } from "./errors";

// ---- create / edit --------------------------------------------------------------------------------------

interface FormState {
  externalRef: string;
  name: string;
  kind: CustomerKind;
  document: string;
  email: string;
  phone: string;
  metadata: string;
}

function initialState(customer: CustomerOut | null): FormState {
  return {
    externalRef: customer?.external_ref ?? "",
    name: customer?.name ?? "",
    kind: customer && isKind(customer.kind) ? customer.kind : "individual",
    document: "",
    email: customer?.email ?? "",
    phone: customer?.phone ?? "",
    metadata: customer && Object.keys(customer.metadata).length > 0 ? JSON.stringify(customer.metadata, null, 2) : "",
  };
}

function parseMetadata(text: string): { value: Record<string, unknown>; error: string | null } {
  if (text.trim() === "") return { value: {}, error: null };
  try {
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
      return { value: parsed as Record<string, unknown>, error: null };
    }
  } catch {
    // fall through to the message below
  }
  return { value: {}, error: t("customers.metadataInvalid") };
}

function validate(state: FormState, editing: boolean): Record<string, string> {
  const errors: Record<string, string> = {};
  if (!editing) {
    const refError = externalRefError(state.externalRef.trim());
    if (refError) errors.external_ref = refError;
  }
  if (state.name.trim() === "") errors.name = t("customers.nameRequired");
  else if (state.name.trim().length > 200) errors.name = t("customers.nameTooLong");
  if (state.document.trim() !== "") {
    const error = documentError(state.document, state.kind);
    if (error) errors.document = error;
  }
  if (state.email.trim() !== "" && !/^[^\s@]+@[^\s@]+$/.test(state.email.trim())) errors.email = t("customers.emailInvalid");
  const metadata = parseMetadata(state.metadata);
  if (metadata.error) errors.metadata = metadata.error;
  return errors;
}

export function CustomerFormDialog({
  open,
  customer,
  projectId,
  onClose,
  onSaved,
}: {
  open: boolean;
  /** The customer being edited, or null to create. */
  customer: CustomerOut | null;
  projectId: string;
  onClose: () => void;
  onSaved: (customer: CustomerOut) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent className="max-w-lg overflow-y-auto">
        <CustomerForm customer={customer} projectId={projectId} onClose={onClose} onSaved={onSaved} />
      </DialogContent>
    </Dialog>
  );
}

// The form lives inside the content so closing the dialog unmounts it, dropping the typed document with it.
function CustomerForm({
  customer,
  projectId,
  onClose,
  onSaved,
}: {
  customer: CustomerOut | null;
  projectId: string;
  onClose: () => void;
  onSaved: (customer: CustomerOut) => void;
}) {
  const toast = useToast();
  const tr = useT();
  const { upsert, isPending } = useUpsertCustomer(projectId);
  const editing = customer !== null;
  const hasDocument = customer?.document_masked != null;
  const [state, setState] = useState<FormState>(() => initialState(customer));
  const [clientErrors, setClientErrors] = useState<Record<string, string>>({});
  const [serverErrors, setServerErrors] = useState<{ fields: Record<string, string>; form: string | null }>({
    fields: {},
    form: null,
  });
  const errors = { ...serverErrors.fields, ...clientErrors };

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setState((current) => ({ ...current, [key]: value }));
    setClientErrors((current) => ({ ...current, [key === "externalRef" ? "external_ref" : key]: "" }));
    setServerErrors((current) => ({ ...current, fields: { ...current.fields, [key === "externalRef" ? "external_ref" : key]: "" } }));
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const found = validate(state, editing);
    setClientErrors(found);
    if (Object.keys(found).length > 0) return;

    const request: UpsertCustomerRequest = {
      name: state.name.trim(),
      kind: state.kind,
      email: state.email.trim() === "" ? null : state.email.trim(),
      phone: state.phone.trim() === "" ? null : state.phone.trim(),
      metadata: parseMetadata(state.metadata).value,
      ...(state.document.trim() === "" ? {} : { document: state.document.trim() }),
    };
    setServerErrors({ fields: {}, form: null });
    try {
      const saved = await upsert(state.externalRef.trim(), request);
      // The document was sent once; forget it before anything else renders.
      setState((current) => ({ ...current, document: "" }));
      toast({ tone: "success", title: editing ? tr("customers.updated") : tr("customers.saved"), description: saved.name });
      onSaved(saved);
    } catch (error) {
      const mapped = fieldErrorsOf(error);
      setServerErrors(mapped);
      setState((current) => ({ ...current, document: "" }));
      if (mapped.form !== null) toast({ tone: "danger", title: tr("customers.saveFailed"), description: mapped.form });
    }
  }

  const show = (name: string) => errors[name] || undefined;

  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-4" noValidate>
      <DialogHeader>
        <DialogTitle>{editing ? tr("customers.formEditTitle") : tr("customers.formNewTitle")}</DialogTitle>
        <DialogDescription>
          {editing ? tr("customers.formEditDescription") : tr("customers.formNewDescription")}
        </DialogDescription>
      </DialogHeader>
      <Field label={tr("customers.byExternalRef")} error={show("external_ref")} hint={tr("customers.refHint")}>
        <Input
          value={state.externalRef}
          onChange={(event) => set("externalRef", event.target.value)}
          disabled={editing}
          autoComplete="off"
          spellCheck={false}
          className="font-mono"
        />
      </Field>
      <Field label={tr("customers.rows.name")} error={show("name")}>
        <Input value={state.name} onChange={(event) => set("name", event.target.value)} autoComplete="off" />
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field
          label={tr("customers.rows.type")}
          error={show("kind")}
          hint={hasDocument ? tr("customers.typeHintFixed") : undefined}
        >
          <Select
            value={state.kind}
            disabled={hasDocument}
            onChange={(event) => {
              if (isKind(event.target.value)) set("kind", event.target.value);
            }}
          >
            {(["individual", "company"] as const).map((kind) => (
              <option key={kind} value={kind}>
                {kindLabel(kind)}
              </option>
            ))}
          </Select>
        </Field>
        <Field
          label={state.kind === "individual" ? "CPF" : "CNPJ"}
          error={show("document")}
          hint={hasDocument ? tr("customers.docOnFile", { masked: customer?.document_masked ?? "" }) : tr("customers.docOptional")}
        >
          <Input
            value={state.document}
            onChange={(event) => set("document", event.target.value)}
            autoComplete="off"
            spellCheck={false}
            inputMode="numeric"
            className="font-mono"
          />
        </Field>
      </div>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label={tr("customers.rows.email")} error={show("email")}>
          <Input type="email" value={state.email} onChange={(event) => set("email", event.target.value)} autoComplete="off" />
        </Field>
        <Field label={tr("customers.rows.phone")} error={show("phone")}>
          <Input type="tel" value={state.phone} onChange={(event) => set("phone", event.target.value)} autoComplete="off" />
        </Field>
      </div>
      <Field label={tr("customers.metadata")} error={show("metadata")} hint={tr("customers.metadataHint")}>
        <Textarea value={state.metadata} onChange={(event) => set("metadata", event.target.value)} spellCheck={false} className="font-mono" />
      </Field>
      {serverErrors.form ? (
        <p role="alert" className="text-sm text-danger">
          {serverErrors.form}
        </p>
      ) : null}
      <DialogFooter>
        <Button type="button" variant="secondary" onClick={onClose}>
          {tr("common.cancel")}
        </Button>
        <Button type="submit" disabled={isPending}>
          {isPending ? tr("customers.saving") : editing ? tr("customers.saveChanges") : tr("customers.saveCustomer")}
        </Button>
      </DialogFooter>
    </form>
  );
}

// ---- anonymize ------------------------------------------------------------------------------------------

export function AnonymizeCustomerDialog({
  customer,
  projectId,
  onClose,
  onDone,
}: {
  customer: CustomerOut | null;
  projectId: string;
  onClose: () => void;
  onDone: (customer: CustomerOut) => void;
}) {
  return (
    <Dialog open={customer !== null} onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent>
        {customer ? <AnonymizeForm customer={customer} projectId={projectId} onClose={onClose} onDone={onDone} /> : null}
      </DialogContent>
    </Dialog>
  );
}

function AnonymizeForm({
  customer,
  projectId,
  onClose,
  onDone,
}: {
  customer: CustomerOut;
  projectId: string;
  onClose: () => void;
  onDone: (customer: CustomerOut) => void;
}) {
  const toast = useToast();
  const tr = useT();
  const { anonymize, isPending } = useAnonymizeCustomer(projectId);
  const [typed, setTyped] = useState("");
  const [failure, setFailure] = useState<string | null>(null);
  const matches = typed === customer.external_ref;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!matches) return;
    setFailure(null);
    try {
      const result = await anonymize(customer.id);
      toast({ tone: "success", title: tr("customers.anonymizedToast"), description: tr("customers.anonymizedToastDescription", { ref: customer.external_ref }) });
      onDone(result);
    } catch (error) {
      setFailure(fieldErrorsOf(error).form ?? tr("customers.anonymizeFailed"));
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-4">
      <DialogHeader>
        <DialogTitle>{tr("customers.anonymizeTitle")}</DialogTitle>
        <DialogDescription>{tr("customers.anonymizeDescription", { name: customer.name })}</DialogDescription>
      </DialogHeader>
      <Field label={tr("customers.typeToConfirm", { ref: customer.external_ref })}>
        <Input
          value={typed}
          onChange={(event) => setTyped(event.target.value)}
          autoComplete="off"
          spellCheck={false}
          className="font-mono"
        />
      </Field>
      {failure ? (
        <p role="alert" className="text-sm text-danger">
          {failure}
        </p>
      ) : null}
      <DialogFooter>
        <Button type="button" variant="secondary" onClick={onClose} disabled={isPending}>
          {tr("common.cancel")}
        </Button>
        <Button type="submit" variant="danger" disabled={!matches || isPending}>
          {isPending ? tr("customers.anonymizing") : tr("customers.anonymizeTitle")}
        </Button>
      </DialogFooter>
    </form>
  );
}
