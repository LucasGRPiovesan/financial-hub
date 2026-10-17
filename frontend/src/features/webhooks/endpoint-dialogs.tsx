import { useState, type FormEvent } from "react";

import type { WebhookEndpointOut } from "../../api/generated/model";
import { Field } from "../../components/form-field";
import { Button } from "../../components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { useToast } from "../../components/ui/use-toast";
import { useT } from "../../i18n";
import { fieldErrorsOf } from "../console/errors";
import { useCreateWebhookEndpoint, useUpdateWebhookEndpoint } from "./webhooks-api";
import { eventTypesOf, selectionOf, useSecretGuard, type EventSelection } from "./selection";
import { EventTypesPicker, SecretPanel } from "./webhook-parts";

interface FormProps {
  initial?: WebhookEndpointOut;
  fieldsError: Record<string, string>;
  formError: string | null;
  pending: boolean;
  submitLabel: string;
  pendingLabel: string;
  title: string;
  description: string;
  onCancel: () => void;
  onSubmit: (request: { url: string; description: string | null; event_types: string[] }) => void;
}

function EndpointForm({ initial, fieldsError, formError, pending, submitLabel, pendingLabel, title, description, onCancel, onSubmit }: FormProps) {
  const t = useT();
  const [url, setUrl] = useState(initial?.url ?? "");
  const [text, setText] = useState(initial?.description ?? "");
  const [selection, setSelection] = useState<EventSelection>(selectionOf(initial?.event_types));
  const [missingEvents, setMissingEvents] = useState(false);
  const eventsError =
    (missingEvents ? t("webhooks.form.eventsRequired") : undefined) ??
    fieldsError.event_types ??
    Object.entries(fieldsError).find(([key]) => key.startsWith("event_types."))?.[1];

  function submit(event: FormEvent) {
    event.preventDefault();
    const eventTypes = eventTypesOf(selection);
    if (eventTypes.length === 0) {
      setMissingEvents(true);
      return;
    }
    setMissingEvents(false);
    onSubmit({ url: url.trim(), description: text.trim() === "" ? null : text.trim(), event_types: eventTypes });
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
      <DialogHeader>
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
      </DialogHeader>
      <Field label={t("webhooks.form.urlLabel")} error={fieldsError.url} hint={t("webhooks.form.urlHint")}>
        <Input type="url" inputMode="url" placeholder="https://example.com/hooks" value={url} onChange={(event) => setUrl(event.target.value)} autoComplete="off" required />
      </Field>
      <Field label={t("webhooks.form.descriptionLabel")} error={fieldsError.description} hint={t("webhooks.form.descriptionHint")}>
        <Input value={text} onChange={(event) => setText(event.target.value)} autoComplete="off" maxLength={200} />
      </Field>
      <EventTypesPicker
        value={selection}
        onChange={(next) => {
          setMissingEvents(false);
          setSelection(next);
        }}
        error={eventsError}
      />
      {formError ? (
        <p role="alert" className="text-sm text-danger">
          {formError}
        </p>
      ) : null}
      <DialogFooter>
        <Button type="button" variant="secondary" onClick={onCancel}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" disabled={pending}>
          {pending ? pendingLabel : submitLabel}
        </Button>
      </DialogFooter>
    </form>
  );
}

/**
 * Create an endpoint, then show its signing secret exactly once.
 * Like the API key dialog: the secret lives only in this component's state, never in the mutation result or the
 * query cache, and the dialog stays open until the operator confirms they saved it.
 */
export function CreateEndpointDialog({ projectId, open, onOpenChange }: { projectId: string; open: boolean; onOpenChange: (open: boolean) => void }) {
  const t = useT();
  const toast = useToast();
  const create = useCreateWebhookEndpoint(projectId);
  const [issued, setIssued] = useState<{ secret: string; url: string } | null>(null);
  const { warn, guard, reset } = useSecretGuard(() => {
    setIssued(null);
    create.reset();
    onOpenChange(false);
  });
  const { fields, form } = fieldErrorsOf(create.error);

  function cancel() {
    create.reset();
    onOpenChange(false);
  }

  return (
    <Dialog open={open} onOpenChange={(next) => (guard(next, issued !== null) ? (next ? onOpenChange(true) : cancel()) : undefined)}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto">
        {issued === null ? (
          <EndpointForm
            fieldsError={fields}
            formError={form}
            pending={create.isPending}
            submitLabel={t("webhooks.create.submit")}
            pendingLabel={t("webhooks.create.submitting")}
            title={t("webhooks.create.title")}
            description={t("webhooks.create.description")}
            onCancel={cancel}
            onSubmit={(request) =>
              create.mutate(
                { request, onSecret: (secret) => setIssued({ secret, url: request.url }) },
                {
                  onSuccess: () => toast({ tone: "success", title: t("webhooks.create.created"), description: request.url }),
                  onError: () => toast({ tone: "danger", title: t("webhooks.create.failed") }),
                },
              )
            }
          />
        ) : (
          <SecretPanel secret={issued.secret} warn={warn} description={t("webhooks.secret.createdDescription", { url: issued.url })} onDone={reset} />
        )}
      </DialogContent>
    </Dialog>
  );
}

export function EditEndpointDialog({
  projectId,
  endpoint,
  open,
  onOpenChange,
}: {
  projectId: string;
  endpoint: WebhookEndpointOut;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  const toast = useToast();
  const update = useUpdateWebhookEndpoint(projectId, endpoint.id);
  const { fields, form } = fieldErrorsOf(update.error);

  function close() {
    update.reset();
    onOpenChange(false);
  }

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? onOpenChange(true) : close())}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto">
        {open ? (
          <EndpointForm
            initial={endpoint}
            fieldsError={fields}
            formError={form}
            pending={update.isPending}
            submitLabel={t("webhooks.edit.submit")}
            pendingLabel={t("webhooks.edit.submitting")}
            title={t("webhooks.edit.title")}
            description={t("webhooks.edit.description")}
            onCancel={close}
            onSubmit={(request) =>
              update.mutate(request, {
                onSuccess: () => {
                  toast({ tone: "success", title: t("webhooks.edit.saved") });
                  close();
                },
                onError: () => toast({ tone: "danger", title: t("webhooks.edit.failed") }),
              })
            }
          />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

/** Shows a rotated secret once; same no-dismiss-until-saved rule as creation. */
export function RotatedSecretDialog({ secret, url, onDone }: { secret: string | null; url: string; onDone: () => void }) {
  const t = useT();
  const { warn, guard, reset } = useSecretGuard(onDone);
  return (
    <Dialog open={secret !== null} onOpenChange={(next) => void guard(next, secret !== null)}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto">
        {secret !== null ? <SecretPanel secret={secret} warn={warn} description={t("webhooks.secret.rotatedDescription", { url })} onDone={reset} /> : null}
      </DialogContent>
    </Dialog>
  );
}
