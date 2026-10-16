import { useState, type FormEvent } from "react";

import type { CreateMetricRequest, MetricOut } from "../../api/generated/model";
import { Field } from "../../components/form-field";
import { Button } from "../../components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Select } from "../../components/ui/select";
import { Textarea } from "../../components/ui/textarea";
import { useToast } from "../../components/ui/use-toast";
import { useI18n } from "../../i18n";
import { errorText, fieldErrorsOf } from "../console/errors";
import { AGGREGATIONS, useMetricMutations, type Aggregation } from "./metrics-api";

const CODE_PATTERN = /^[a-z][a-z0-9_]{0,62}$/;
const NAME_MAX = 100;

function isAggregation(value: string): value is Aggregation {
  return AGGREGATIONS.some((aggregation) => aggregation === value);
}

interface FormState {
  code: string;
  name: string;
  description: string;
  aggregation: Aggregation;
  field: string;
}

const EMPTY: FormState = { code: "", name: "", description: "", aggregation: "sum", field: "" };

export function CreateMetricDialog({ open, projectId, onClose }: { open: boolean; projectId: string; onClose: () => void }) {
  return (
    <Dialog open={open} onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent>{open ? <CreateMetricForm projectId={projectId} onClose={onClose} /> : null}</DialogContent>
    </Dialog>
  );
}

// Mounted only while open, so closing drops the typed values.
function CreateMetricForm({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const { t, tLoose } = useI18n();
  const toast = useToast();
  const { create, isPending } = useMetricMutations(projectId);
  const [state, setState] = useState<FormState>(EMPTY);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const needsField = state.aggregation !== "count";

  function validate(): Record<string, string> {
    const found: Record<string, string> = {};
    if (state.code === "") found.code = t("billing.create.errors.codeRequired");
    else if (!CODE_PATTERN.test(state.code)) found.code = t("billing.create.errors.codeInvalid");
    if (state.name.trim() === "") found.name = t("billing.create.errors.nameRequired");
    else if (state.name.trim().length > NAME_MAX) found.name = t("billing.create.errors.nameTooLong");
    if (needsField && state.field.trim() === "") found.field = t("billing.create.errors.fieldRequired");
    return found;
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const found = validate();
    setErrors(found);
    setFormError(null);
    if (Object.keys(found).length > 0) return;
    const request: CreateMetricRequest = {
      code: state.code,
      name: state.name.trim(),
      aggregation: state.aggregation,
      description: state.description.trim() === "" ? null : state.description.trim(),
      field: needsField ? state.field.trim() : null,
    };
    try {
      const created = await create(request);
      toast({ tone: "success", title: t("billing.create.done"), description: t("billing.create.doneDescription", { code: created.code }) });
      onClose();
    } catch (error) {
      const mapped = fieldErrorsOf(error);
      setErrors(mapped.fields);
      const message = mapped.form ?? (Object.keys(mapped.fields).length > 0 ? null : errorText(error));
      setFormError(message);
      if (message) toast({ tone: "danger", title: t("billing.create.failed"), description: message });
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-4" noValidate>
      <DialogHeader>
        <DialogTitle>{t("billing.create.title")}</DialogTitle>
        <DialogDescription>{t("billing.create.description")}</DialogDescription>
      </DialogHeader>
      <Field label={t("billing.create.code")} error={errors.code} hint={t("billing.create.codeHint")}>
        <Input
          value={state.code}
          onChange={(event) => setState({ ...state, code: event.target.value })}
          autoComplete="off"
          spellCheck={false}
          maxLength={63}
          className="font-mono"
          placeholder="api_calls"
        />
      </Field>
      <Field label={t("billing.create.name")} error={errors.name}>
        <Input value={state.name} onChange={(event) => setState({ ...state, name: event.target.value })} autoComplete="off" maxLength={NAME_MAX + 20} />
      </Field>
      <Field label={t("billing.create.descriptionLabel")} error={errors.description}>
        <Textarea value={state.description} onChange={(event) => setState({ ...state, description: event.target.value })} rows={2} />
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label={t("billing.create.aggregation")} error={errors.aggregation}>
          <Select
            value={state.aggregation}
            onChange={(event) => {
              const next = event.target.value;
              if (isAggregation(next)) setState({ ...state, aggregation: next, field: next === "count" ? "" : state.field });
            }}
          >
            {AGGREGATIONS.map((aggregation) => (
              <option key={aggregation} value={aggregation}>
                {tLoose("billing.metrics.aggregation", aggregation)}
              </option>
            ))}
          </Select>
        </Field>
        <Field label={t("billing.create.field")} error={errors.field} hint={needsField ? t("billing.create.fieldHint") : t("billing.create.fieldNotNeeded")}>
          <Input
            value={state.field}
            onChange={(event) => setState({ ...state, field: event.target.value })}
            disabled={!needsField}
            autoComplete="off"
            spellCheck={false}
            className="font-mono"
            placeholder={needsField ? "bytes" : ""}
          />
        </Field>
      </div>
      {formError ? (
        <p role="alert" className="text-sm text-danger">
          {formError}
        </p>
      ) : null}
      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onClose} disabled={isPending}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" disabled={isPending}>
          {isPending ? t("billing.create.submitting") : t("billing.create.submit")}
        </Button>
      </DialogFooter>
    </form>
  );
}

export function ArchiveMetricDialog({ metric, projectId, onClose }: { metric: MetricOut | null; projectId: string; onClose: () => void }) {
  return (
    <Dialog open={metric !== null} onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent>{metric ? <ArchiveForm metric={metric} projectId={projectId} onClose={onClose} /> : null}</DialogContent>
    </Dialog>
  );
}

function ArchiveForm({ metric, projectId, onClose }: { metric: MetricOut; projectId: string; onClose: () => void }) {
  const { t } = useI18n();
  const toast = useToast();
  const { archive, isPending } = useMetricMutations(projectId);
  const [typed, setTyped] = useState("");
  const [failure, setFailure] = useState<string | null>(null);
  const matches = typed === metric.code;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!matches) return;
    setFailure(null);
    try {
      await archive(metric.code);
      toast({ tone: "success", title: t("billing.archive.done"), description: t("billing.archive.doneDescription", { code: metric.code }) });
      onClose();
    } catch (error) {
      const message = fieldErrorsOf(error).form ?? t("billing.archive.failed");
      setFailure(message);
      toast({ tone: "danger", title: t("billing.archive.failed"), description: message });
    }
  }

  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-4">
      <DialogHeader>
        <DialogTitle>{t("billing.archive.title")}</DialogTitle>
        <DialogDescription>{t("billing.archive.description", { code: metric.code })}</DialogDescription>
      </DialogHeader>
      <Field label={t("billing.archive.confirmLabel")}>
        <Input value={typed} onChange={(event) => setTyped(event.target.value)} autoComplete="off" spellCheck={false} className="font-mono" />
      </Field>
      {failure ? (
        <p role="alert" className="text-sm text-danger">
          {failure}
        </p>
      ) : null}
      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onClose} disabled={isPending}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" variant="danger" disabled={!matches || isPending}>
          {isPending ? t("billing.archive.submitting") : t("billing.archive.submit")}
        </Button>
      </DialogFooter>
    </form>
  );
}
