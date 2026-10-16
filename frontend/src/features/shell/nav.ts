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

import type { MessageKey } from "../../i18n";
import type { Scope } from "../../lib/scope";

export interface NavItem {
  /** Route path. */
  to: string;
  /** Translation keys, resolved at display time so the sidebar follows the language. */
  labelKey: MessageKey;
  icon: LucideIcon;
  scope: Scope;
  /** What this area will do; shown on its placeholder page. */
  summaryKey: MessageKey;
}

/** Sidebar order; blank lines in the source mark the visual groups (see NAV_GROUPS). */
export const NAV_GROUPS: readonly (readonly NavItem[])[] = [
  [
    {
      to: "/",
      labelKey: "nav.overview.label",
      icon: LayoutDashboard,
      scope: "implemented",
      summaryKey: "nav.overview.summary",
    },
  ],
  [
    {
      to: "/projects",
      labelKey: "nav.projects.label",
      icon: FolderKanban,
      scope: "implemented",
      summaryKey: "nav.projects.summary",
    },
    {
      to: "/customers",
      labelKey: "nav.customers.label",
      icon: Users,
      scope: "implemented",
      summaryKey: "nav.customers.summary",
    },
  ],
  [
    {
      to: "/ledger",
      labelKey: "nav.ledger.label",
      icon: BookOpenText,
      scope: "planned",
      summaryKey: "nav.ledger.summary",
    },
    {
      to: "/payments",
      labelKey: "nav.payments.label",
      icon: CreditCard,
      scope: "planned",
      summaryKey: "nav.payments.summary",
    },
    {
      to: "/billing",
      labelKey: "nav.billing.label",
      icon: Receipt,
      scope: "planned",
      summaryKey: "nav.billing.summary",
    },
  ],
  [
    {
      to: "/integrations",
      labelKey: "nav.integrations.label",
      icon: Plug,
      scope: "sandbox",
      summaryKey: "nav.integrations.summary",
    },
    {
      to: "/events",
      labelKey: "nav.events.label",
      icon: Activity,
      scope: "planned",
      summaryKey: "nav.events.summary",
    },
    {
      to: "/webhooks",
      labelKey: "nav.webhooks.label",
      icon: Webhook,
      scope: "planned",
      summaryKey: "nav.webhooks.summary",
    },
  ],
  [
    {
      to: "/reconciliation",
      labelKey: "nav.reconciliation.label",
      icon: Scale,
      scope: "planned",
      summaryKey: "nav.reconciliation.summary",
    },
    {
      to: "/audit",
      labelKey: "nav.audit.label",
      icon: ScrollText,
      scope: "planned",
      summaryKey: "nav.audit.summary",
    },
  ],
  [
    {
      to: "/api-keys",
      labelKey: "nav.apiKeys.label",
      icon: KeyRound,
      scope: "implemented",
      summaryKey: "nav.apiKeys.summary",
    },
    {
      to: "/members",
      labelKey: "nav.members.label",
      icon: UserCog,
      scope: "implemented",
      summaryKey: "nav.members.summary",
    },
    {
      to: "/settings",
      labelKey: "nav.settings.label",
      icon: Settings,
      scope: "planned",
      summaryKey: "nav.settings.summary",
    },
  ],
];

export const NAV_ITEMS: readonly NavItem[] = NAV_GROUPS.flat();
