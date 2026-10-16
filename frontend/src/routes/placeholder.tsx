import { PageHeader } from "../components/page-header";
import { ScopeBadge } from "../components/scope-badge";
import { EmptyState } from "../components/ui/empty-state";
import { useT } from "../i18n";
import type { NavItem } from "../features/shell/nav";

/** Stand-in for an area whose screens have not been built yet. */
export function PlaceholderPage({ item }: { item: NavItem }) {
  const t = useT();
  return (
    <>
      <PageHeader title={t(item.labelKey)} />
      <EmptyState
        icon={item.icon}
        title={t("placeholder.unavailable", { name: t(item.labelKey) })}
        status={<ScopeBadge scope="planned" />}
        description={t(item.summaryKey)}
      />
    </>
  );
}
