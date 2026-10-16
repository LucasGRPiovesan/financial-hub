import { useSuspenseQuery } from "@tanstack/react-query";
import { Outlet } from "@tanstack/react-router";
import { Menu } from "lucide-react";
import { useState } from "react";

import { Button } from "../../components/ui/button";
import { DialogDescription, DialogTitle, Sheet, SheetContent, SheetTrigger } from "../../components/ui/dialog";
import { meQueryOptions } from "../auth/auth-api";
import { ProjectProvider } from "../console/project-context";
import { BrandMark, SidebarNav } from "./sidebar-nav";
import { OperatorMenu, ProjectSwitcher, ThemeMenu } from "./top-bar";

export function AppShell() {
  const { data: me } = useSuspenseQuery(meQueryOptions);
  const [navOpen, setNavOpen] = useState(false);

  return (
    <ProjectProvider memberships={me.memberships}>
    <div className="min-h-dvh lg:grid lg:grid-cols-[15rem_minmax(0,1fr)]">
      <a
        href="#main"
        className="sr-only z-[70] rounded-sm bg-primary px-3 py-2 text-sm font-medium text-primary-ink focus:not-sr-only focus:fixed focus:top-2 focus:left-2"
      >
        Skip to content
      </a>

      <aside className="sticky top-0 hidden h-dvh flex-col bg-chrome lg:flex">
        <div className="px-5 py-5">
          <BrandMark />
        </div>
        <SidebarNav />
      </aside>

      <div className="flex min-w-0 flex-col">
        <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-line bg-surface px-3 sm:px-5">
          <Sheet open={navOpen} onOpenChange={setNavOpen}>
            <SheetTrigger asChild>
              <Button variant="ghost" size="icon" className="lg:hidden" aria-label="Open navigation">
                <Menu aria-hidden="true" />
              </Button>
            </SheetTrigger>
            <SheetContent side="left" hideClose className="bg-chrome text-chrome-ink" aria-describedby={undefined}>
              <DialogTitle className="sr-only">Navigation</DialogTitle>
              <DialogDescription className="sr-only">Go to a section of the console.</DialogDescription>
              <div className="px-5 pt-5">
                <BrandMark />
              </div>
              <SidebarNav onNavigate={() => setNavOpen(false)} />
            </SheetContent>
          </Sheet>

          <ProjectSwitcher memberships={me.memberships} />
          <div className="ml-auto flex items-center gap-1">
            <ThemeMenu />
            <OperatorMenu operator={me.operator} />
          </div>
        </header>

        <main id="main" tabIndex={-1} className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 outline-none sm:px-8 sm:py-8">
          <Outlet />
        </main>
      </div>
    </div>
    </ProjectProvider>
  );
}
