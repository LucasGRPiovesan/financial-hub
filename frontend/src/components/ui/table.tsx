import type { HTMLAttributes, TdHTMLAttributes, ThHTMLAttributes } from "react";

import { cn } from "../../lib/cn";

/**
 * The scrolling wrapper keeps wide ledgers usable on narrow screens. With `maxHeight` it also scrolls
 * vertically and the header row sticks to the top of the scroll area.
 */
export function Table({
  className,
  maxHeight,
  regionLabel,
  ...props
}: HTMLAttributes<HTMLTableElement> & { maxHeight?: string; regionLabel?: string }) {
  return (
    <div
      className={cn("w-full overflow-x-auto", maxHeight && "overflow-y-auto")}
      style={maxHeight ? { maxHeight } : undefined}
      // A scrollable region must be reachable by keyboard.
      tabIndex={maxHeight ? 0 : undefined}
      role={maxHeight ? "region" : undefined}
      aria-label={maxHeight ? regionLabel : undefined}
    >
      <table className={cn("w-full border-collapse text-sm", className)} {...props} />
    </div>
  );
}

/** Hairline rows, no zebra: the double rule under the header is the only heavy line. */
export function TableHeader({ className, ...props }: HTMLAttributes<HTMLTableSectionElement>) {
  return (
    <thead
      className={cn(
        "sticky top-0 z-10 bg-surface shadow-[inset_0_-1px_0_0_var(--color-line-strong),inset_0_-3px_0_0_var(--color-surface),inset_0_-4px_0_0_var(--color-line-strong)]",
        className,
      )}
      {...props}
    />
  );
}

export function TableBody({ className, ...props }: HTMLAttributes<HTMLTableSectionElement>) {
  return <tbody className={cn("[&_tr:last-child]:border-0", className)} {...props} />;
}

export function TableRow({ className, ...props }: HTMLAttributes<HTMLTableRowElement>) {
  return <tr className={cn("motion-ui border-b border-line hover:bg-surface-sunken", className)} {...props} />;
}

interface CellAlign {
  /** Right-aligned tabular figures, for amounts and counts. */
  numeric?: boolean;
}

export function TableHead({ className, numeric, ...props }: ThHTMLAttributes<HTMLTableCellElement> & CellAlign) {
  return (
    <th
      scope="col"
      className={cn(
        "h-10 px-3 text-left align-middle text-xs font-semibold text-ink-muted",
        numeric && "text-right",
        className,
      )}
      {...props}
    />
  );
}

export function TableCell({ className, numeric, ...props }: TdHTMLAttributes<HTMLTableCellElement> & CellAlign) {
  return <td className={cn("px-3 py-2.5 align-middle", numeric && "figures text-right", className)} {...props} />;
}
