import { cva } from "class-variance-authority";

export const buttonVariants = cva(
  "inline-flex shrink-0 items-center justify-center gap-2 rounded-sm font-medium whitespace-nowrap motion-ui select-none active:translate-y-px disabled:pointer-events-none disabled:opacity-50 aria-disabled:pointer-events-none aria-disabled:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        primary: "bg-primary text-primary-ink shadow-raised hover:bg-primary-hover",
        secondary: "border border-control bg-surface text-ink shadow-raised hover:bg-surface-sunken",
        ghost: "text-ink hover:bg-surface-sunken",
        danger: "bg-danger text-primary-ink shadow-raised hover:opacity-90",
        link: "h-auto px-0 text-primary underline underline-offset-4 hover:text-primary-hover",
      },
      size: {
        sm: "h-8 px-2.5 text-sm",
        md: "h-9 px-3.5 text-sm",
        lg: "h-11 px-5 text-base",
        icon: "size-9",
      },
    },
    defaultVariants: { variant: "primary", size: "md" },
  },
);
