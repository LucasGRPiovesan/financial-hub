import { useSuspenseQuery } from "@tanstack/react-query";
import { Outlet, useRouterState } from "@tanstack/react-router";
import { Menu } from "lucide-react";
import { useState } from "react";

import { Button } from "../../components/ui/button";
import { DialogDescription, DialogTitle, Sheet, SheetContent, SheetTrigger } from "../../components/ui/dialog";
import { meQueryOptions } from "../auth/auth-api";
import { useT } from "../../i18n";
import { useDocumentTitle } from "./document-title";
import { NAV_ITEMS } from "./nav";
import { ProjectProvider } from "../console/project-context";
import { BrandMark, SidebarNav } from "./sidebar-nav";
import { Breadcrumbs, OperatorMenu, ProjectSwitcher, ThemeMenu } from "./top-bar";

export function AppShell() {
  const { data: me } = useSuspenseQuery(meQueryOptions);
  const [navOpen, setNavOpen] = useState(false);
  const t = useT();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const current = NAV_ITEMS.find((item) => (item.to === "/" ? pathname === "/" : pathname.startsWith(item.to)));
  useDocumentTitle(current ? t(current.labelKey) : null);

  return (
    <ProjectProvider memberships={me.memberships}>
    <div className="min-h-dvh lg:grid lg:grid-cols-[15rem_minmax(0,1fr)]">
      <a
        href="#main"
        className="sr-only z-[70] rounded-sm bg-primary px-3 py-2 text-sm font-medium text-primary-ink focus:not-sr-only focus:fixed focus:top-2 focus:left-2"
      >
        {t("shell.skipToContent")}
      </a>

      <aside className="vt-sidebar sticky top-0 z-20 hidden h-dvh flex-col bg-chrome lg:flex">
        <div className="px-5 py-5">
          <BrandMark />
        </div>
        <SidebarNav />
        <p className="mt-auto border-t border-chrome-line px-5 py-4 text-xs leading-relaxed text-chrome-muted">
          {t("shell.evalNotice")}
        </p>
      </aside>

      <div className="flex min-w-0 flex-col">
        <header className="vt-header sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-line bg-surface px-3 sm:px-5">
          <Sheet open={navOpen} onOpenChange={setNavOpen}>
            <SheetTrigger asChild>
              <Button variant="ghost" size="icon" className="lg:hidden" aria-label={t("shell.openNav")}>
                <Menu aria-hidden="true" />
              </Button>
            </SheetTrigger>
            <SheetContent side="left" hideClose className="bg-chrome text-chrome-ink" aria-describedby={undefined}>
              <DialogTitle className="sr-only">{t("shell.navTitle")}</DialogTitle>
              <DialogDescription className="sr-only">{t("shell.navDescription")}</DialogDescription>
              <div className="px-5 pt-5">
                <BrandMark />
              </div>
              <SidebarNav onNavigate={() => setNavOpen(false)} />
            </SheetContent>
          </Sheet>

          <ProjectSwitcher memberships={me.memberships} />
          <Breadcrumbs />
          <div className="ml-auto flex items-center gap-1">
            <ThemeMenu />
            <OperatorMenu operator={me.operator} />
          </div>
        </header>

        <main
          id="main"
          tabIndex={-1}
          className="vt-page w-full min-w-0 flex-1 px-4 py-6 outline-none sm:px-6 sm:py-8 xl:px-8 2xl:px-10"
        >
          <div key={pathname} className="route-enter">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
    </ProjectProvider>
  );
}
