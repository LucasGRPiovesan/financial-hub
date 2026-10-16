import * as MenuPrimitive from "@radix-ui/react-dropdown-menu";
import { Circle } from "lucide-react";
import type { ComponentProps } from "react";

import { cn } from "../../lib/cn";

export const DropdownMenu = MenuPrimitive.Root;
export const DropdownMenuTrigger = MenuPrimitive.Trigger;
export const DropdownMenuRadioGroup = MenuPrimitive.RadioGroup;

export function DropdownMenuContent({
  className,
  sideOffset = 6,
  ...props
}: ComponentProps<typeof MenuPrimitive.Content>) {
  return (
    <MenuPrimitive.Portal>
      <MenuPrimitive.Content
        sideOffset={sideOffset}
        className={cn(
          "z-50 min-w-48 rounded-md data-[state=closed]:animate-pop-out data-[state=open]:animate-pop-in border border-line bg-surface p-1 text-sm text-ink shadow-overlay",
          className,
        )}
        {...props}
      />
    </MenuPrimitive.Portal>
  );
}

const itemClass =
  "motion-ui relative flex cursor-default items-center gap-2 rounded-xs px-2 py-1.5 outline-none select-none data-[disabled]:opacity-50 data-[highlighted]:bg-primary-soft [&_svg]:size-4 [&_svg]:shrink-0";

export function DropdownMenuItem({ className, ...props }: ComponentProps<typeof MenuPrimitive.Item>) {
  return <MenuPrimitive.Item className={cn(itemClass, className)} {...props} />;
}

export function DropdownMenuRadioItem({
  className,
  children,
  ...props
}: ComponentProps<typeof MenuPrimitive.RadioItem>) {
  return (
    <MenuPrimitive.RadioItem className={cn(itemClass, "pl-8", className)} {...props}>
      <span className="absolute left-2 grid size-4 place-items-center">
        <MenuPrimitive.ItemIndicator>
          <Circle className="size-2 fill-current" aria-hidden="true" />
        </MenuPrimitive.ItemIndicator>
      </span>
      {children}
    </MenuPrimitive.RadioItem>
  );
}

export function DropdownMenuLabel({ className, ...props }: ComponentProps<typeof MenuPrimitive.Label>) {
  return <MenuPrimitive.Label className={cn("px-2 py-1.5 text-xs font-medium text-ink-muted", className)} {...props} />;
}

export function DropdownMenuSeparator({ className, ...props }: ComponentProps<typeof MenuPrimitive.Separator>) {
  return <MenuPrimitive.Separator className={cn("-mx-1 my-1 h-px bg-line", className)} {...props} />;
}
