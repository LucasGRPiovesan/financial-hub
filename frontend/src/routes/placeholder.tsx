import { PageHeader } from "../components/page-header";
import { ScopeBadge } from "../components/scope-badge";
import { EmptyState } from "../components/ui/empty-state";
import type { NavItem } from "../features/shell/nav";

/** Stand-in for an area whose screens have not been built yet. */
export function PlaceholderPage({ item }: { item: NavItem }) {
  return (
    <>
      <PageHeader title={item.label} />
      <EmptyState
        icon={item.icon}
        title={`${item.label} isn't available in the console yet`}
        status={<ScopeBadge scope="planned" />}
        description={item.summary}
      />
    </>
  );
}
