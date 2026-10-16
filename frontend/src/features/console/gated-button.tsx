import type { ComponentProps } from "react";

import { Button } from "../../components/ui/button";

interface GatedButtonProps extends ComponentProps<typeof Button> {
  /** False disables the button and explains why on hover (backend still enforces every permission). */
  allowed: boolean;
  reason: string;
}

/** A button the current role may not use: shown disabled with the reason as a tooltip rather than hidden. */
export function GatedButton({ allowed, reason, disabled, ...props }: GatedButtonProps) {
  if (allowed) return <Button disabled={disabled} {...props} />;
  return (
    <span title={reason} className="inline-flex">
      <Button {...props} disabled aria-label={props["aria-label"]} />
    </span>
  );
}
