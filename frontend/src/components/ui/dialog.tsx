import * as DialogPrimitive from "@radix-ui/react-dialog";
import { cva, type VariantProps } from "class-variance-authority";
import { X } from "lucide-react";
import type { ComponentProps, HTMLAttributes } from "react";

import { useT } from "../../i18n";
import { cn } from "../../lib/cn";

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;
export const Sheet = DialogPrimitive.Root;
export const SheetTrigger = DialogPrimitive.Trigger;
export const SheetClose = DialogPrimitive.Close;

function Overlay({ className, ...props }: ComponentProps<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay className={cn("fixed inset-0 z-40 bg-overlay data-[state=closed]:animate-fade-out data-[state=open]:animate-fade-in", className)} {...props} />
  );
}

function CloseButton({ className }: { className?: string }) {
  const t = useT();
  return (
    <DialogPrimitive.Close
      className={cn(
        "motion-ui absolute top-3 right-3 grid size-8 place-items-center rounded-sm text-ink-muted hover:bg-surface-sunken hover:text-ink",
        className,
      )}
    >
      <X className="size-4" aria-hidden="true" />
      <span className="sr-only">{t("common.close")}</span>
    </DialogPrimitive.Close>
  );
}

export function DialogContent({ className, children, ...props }: ComponentProps<typeof DialogPrimitive.Content>) {
  return (
    <DialogPrimitive.Portal>
      <Overlay />
      <DialogPrimitive.Content
        className={cn(
          "fixed top-1/2 left-1/2 z-50 flex max-h-[calc(100dvh-2rem)] w-[calc(100vw-2rem)] overflow-y-auto overscroll-contain max-w-md -translate-x-1/2 -translate-y-1/2 flex-col data-[state=closed]:animate-pop-out data-[state=open]:animate-pop-in gap-4 rounded-lg border border-line bg-surface p-6 text-ink shadow-overlay",
          className,
        )}
        {...props}
      >
        {children}
        <CloseButton />
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

const sheetVariants = cva(
  "fixed inset-y-0 z-50 flex h-full w-[min(20rem,calc(100vw-2rem))] flex-col gap-4 shadow-overlay",
  {
    variants: {
      side: {
        left: "left-0 data-[state=closed]:animate-slide-out-left data-[state=open]:animate-slide-in-left",
        right: "right-0 data-[state=closed]:animate-slide-out-right data-[state=open]:animate-slide-in-right border-l border-line bg-surface text-ink",
      },
    },
    defaultVariants: { side: "right" },
  },
);

export interface SheetContentProps
  extends ComponentProps<typeof DialogPrimitive.Content>,
    VariantProps<typeof sheetVariants> {
  /** Sheets that bring their own chrome (the mobile nav) can supply their own close control. */
  hideClose?: boolean;
}

/** A Sheet is a Dialog anchored to a screen edge. */
export function SheetContent({ className, children, side, hideClose, ...props }: SheetContentProps) {
  return (
    <DialogPrimitive.Portal>
      <Overlay />
      <DialogPrimitive.Content className={cn(sheetVariants({ side }), className)} {...props}>
        {children}
        {hideClose ? null : <CloseButton />}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

export function DialogHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col gap-1.5 pr-8", className)} {...props} />;
}

export function DialogFooter({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  // Sticks to the bottom of a dialog that scrolls, so the actions stay reachable on short viewports.
  return (
    <div
      className={cn(
        "sticky bottom-0 -mx-6 -mb-6 flex flex-col-reverse gap-2 border-t border-line bg-surface px-6 py-4 sm:flex-row sm:justify-end",
        className,
      )}
      {...props}
    />
  );
}

export function DialogTitle({ className, ...props }: ComponentProps<typeof DialogPrimitive.Title>) {
  return <DialogPrimitive.Title className={cn("text-lg font-semibold tracking-tight", className)} {...props} />;
}

export function DialogDescription({ className, ...props }: ComponentProps<typeof DialogPrimitive.Description>) {
  return <DialogPrimitive.Description className={cn("text-sm text-ink-muted", className)} {...props} />;
}
