import { useId, type ReactElement, type ReactNode } from "react";
import { cloneElement } from "react";

import { Label } from "./ui/label";

interface FieldProps {
  label: string;
  /** Field-level error (e.g. mapped from a problem+json `errors[]` entry). */
  error?: string | undefined;
  hint?: ReactNode;
  /** The single control; it receives `id`, `aria-invalid` and `aria-describedby`. */
  children: ReactElement<{ id?: string; "aria-invalid"?: boolean; "aria-describedby"?: string }>;
}

/** Label + control + hint + error, wired together for assistive technology. */
export function Field({ label, error, hint, children }: FieldProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = [hint ? hintId : null, error ? errorId : null].filter(Boolean).join(" ") || undefined;
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {cloneElement(children, { id, "aria-invalid": error ? true : undefined, "aria-describedby": describedBy })}
      {hint ? (
        <p id={hintId} className="text-xs text-ink-muted">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} className="text-xs text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}
