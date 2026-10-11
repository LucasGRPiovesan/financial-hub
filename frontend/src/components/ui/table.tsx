import type { HTMLAttributes, TdHTMLAttributes, ThHTMLAttributes } from "react";

import { cn } from "../../lib/cn";

/** The scrolling wrapper keeps wide ledgers usable on narrow screens. */
export function Table({ className, ...props }: HTMLAttributes<HTMLTableElement>) {
  return (
    <div className="w-full overflow-x-auto">
      <table className={cn("w-full border-collapse text-sm", className)} {...props} />
    </div>
  );
}

export function TableHeader({ className, ...props }: HTMLAttributes<HTMLTableSectionElement>) {
  return <thead className={cn("border-b-[3px] border-double border-line-strong", className)} {...props} />;
}

export function TableBody({ className, ...props }: HTMLAttributes<HTMLTableSectionElement>) {
  return <tbody className={cn("[&_tr:last-child]:border-0", className)} {...props} />;
}

export function TableRow({ className, ...props }: HTMLAttributes<HTMLTableRowElement>) {
  return <tr className={cn("border-b border-line hover:bg-surface-sunken", className)} {...props} />;
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
