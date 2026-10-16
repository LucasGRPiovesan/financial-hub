import {
  Activity,
  BookOpenText,
  CreditCard,
  FolderKanban,
  KeyRound,
  LayoutDashboard,
  Plug,
  Receipt,
  Scale,
  ScrollText,
  Settings,
  UserCog,
  Users,
  Webhook,
  type LucideIcon,
} from "lucide-react";

import type { Scope } from "../../lib/scope";

export interface NavItem {
  /** Route path. */
  to: string;
  label: string;
  icon: LucideIcon;
  scope: Scope;
  /** What this area will do; shown on its placeholder page. */
  summary: string;
}

/** Sidebar order; blank lines in the source mark the visual groups (see NAV_GROUPS). */
export const NAV_GROUPS: readonly (readonly NavItem[])[] = [
  [
    {
      to: "/",
      label: "Overview",
      icon: LayoutDashboard,
      scope: "implemented",
      summary: "Environment status and what each capability label means.",
    },
  ],
  [
    {
      to: "/projects",
      label: "Projects",
      icon: FolderKanban,
      scope: "implemented",
      summary: "Create projects and manage which operators can access them.",
    },
    {
      to: "/customers",
      label: "Customers",
      icon: Users,
      scope: "implemented",
      summary: "Find customers by your own reference and keep their identity data tidy.",
    },
  ],
  [
    {
      to: "/ledger",
      label: "Ledger",
      icon: BookOpenText,
      scope: "planned",
      summary: "Browse accounts, entries and balances of the double-entry ledger.",
    },
    {
      to: "/payments",
      label: "Payments",
      icon: CreditCard,
      scope: "planned",
      summary: "Follow charges, refunds and payouts from creation to settlement.",
    },
    {
      to: "/billing",
      label: "Billing",
      icon: Receipt,
      scope: "planned",
      summary: "Manage plans, subscriptions, usage and invoices.",
    },
  ],
  [
    {
      to: "/integrations",
      label: "Provider accounts",
      icon: Plug,
      scope: "sandbox",
      summary: "Connect payment providers and check their health.",
    },
    {
      to: "/events",
      label: "Events",
      icon: Activity,
      scope: "planned",
      summary: "Inspect the event log and its delivery status.",
    },
    {
      to: "/webhooks",
      label: "Webhooks",
      icon: Webhook,
      scope: "planned",
      summary: "Manage endpoints that receive events and review their deliveries.",
    },
  ],
  [
    {
      to: "/reconciliation",
      label: "Reconciliation",
      icon: Scale,
      scope: "planned",
      summary: "Compare the ledger with provider statements and resolve differences.",
    },
    {
      to: "/audit",
      label: "Audit",
      icon: ScrollText,
      scope: "planned",
      summary: "Review who did what, and when.",
    },
  ],
  [
    {
      to: "/api-keys",
      label: "API keys",
      icon: KeyRound,
      scope: "implemented",
      summary: "Create and revoke the keys your backend uses to call the API.",
    },
    {
      to: "/members",
      label: "Members",
      icon: UserCog,
      scope: "implemented",
      summary: "See who can access the project and what each role allows.",
    },
    {
      to: "/settings",
      label: "Settings",
      icon: Settings,
      scope: "planned",
      summary: "Project configuration, API keys and operator preferences.",
    },
  ],
];

export const NAV_ITEMS: readonly NavItem[] = NAV_GROUPS.flat();
