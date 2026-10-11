import * as ToastPrimitive from "@radix-ui/react-toast";
import { CircleAlert, CircleCheck, Info, X } from "lucide-react";
import { useCallback, useRef, useState, type ReactNode } from "react";

import { cn } from "../../lib/cn";
import { ToastContext, type ToastInput, type ToastTone } from "./use-toast";

interface ToastItem extends ToastInput {
  id: number;
}

const ICONS = { info: Info, success: CircleCheck, danger: CircleAlert } as const;
const TONES: Record<ToastTone, string> = {
  info: "text-info",
  success: "text-success",
  danger: "text-danger",
};

/** Mount once near the root. Errors use the assertive "foreground" type, everything else is announced politely. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const toast = useCallback((input: ToastInput) => {
    const id = nextId.current;
    nextId.current += 1;
    setItems((current) => [...current, { ...input, id }]);
  }, []);

  return (
    <ToastContext value={toast}>
      <ToastPrimitive.Provider swipeDirection="right" duration={6000} label="Notifications">
        {children}
        {items.map((item) => {
          const tone = item.tone ?? "info";
          const Icon = ICONS[tone];
          return (
            <ToastPrimitive.Root
              key={item.id}
              type={tone === "danger" ? "foreground" : "background"}
              onOpenChange={(open) => {
                if (!open) setItems((current) => current.filter((candidate) => candidate.id !== item.id));
              }}
              className="flex w-full animate-toast-in items-start gap-3 rounded-md border border-line bg-surface p-3.5 text-ink shadow-overlay"
            >
              <Icon className={cn("mt-0.5 size-4 shrink-0", TONES[tone])} aria-hidden="true" />
              <div className="min-w-0 flex-1">
                <ToastPrimitive.Title className="text-sm font-medium">{item.title}</ToastPrimitive.Title>
                {item.description ? (
                  <ToastPrimitive.Description className="mt-0.5 text-sm text-ink-muted">
                    {item.description}
                  </ToastPrimitive.Description>
                ) : null}
              </div>
              <ToastPrimitive.Close
                aria-label="Dismiss notification"
                className="grid size-6 shrink-0 place-items-center rounded-xs text-ink-muted hover:bg-surface-sunken hover:text-ink"
              >
                <X className="size-3.5" aria-hidden="true" />
              </ToastPrimitive.Close>
            </ToastPrimitive.Root>
          );
        })}
        <ToastPrimitive.Viewport className="fixed right-4 bottom-4 z-[60] flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2 outline-none" />
      </ToastPrimitive.Provider>
    </ToastContext>
  );
}
