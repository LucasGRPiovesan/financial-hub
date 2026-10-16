import { useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useNavigate } from "@tanstack/react-router";
import { ChevronsUpDown, LogOut, Monitor, Moon, Sun } from "lucide-react";
import { useState } from "react";

import { LanguageRadioGroup } from "../../components/language-menu";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../../components/ui/dropdown-menu";
import { useToast } from "../../components/ui/use-toast";
import { useI18n } from "../../i18n";
import { isApiError } from "../../lib/api";
import { isThemePreference, useTheme } from "../../lib/theme";
import { useActiveMembership } from "../../lib/project-selection";
import { logout, type Membership, type Operator } from "../auth/auth-api";
import { clearSession } from "../auth/session";
import { useProject } from "../console/use-project";
import { roleTone } from "../console/roles";
import { NAV_ITEMS } from "./nav";

export function ProjectSwitcher({ memberships }: { memberships: readonly Membership[] }) {
  const { active, select } = useActiveMembership(memberships);
  const { t, tLoose } = useI18n();

  if (!active) {
    return (
      <Button variant="secondary" disabled aria-label={t("shell.project")}>
        {t("shell.noProjects")}
      </Button>
    );
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="secondary" aria-label={t("shell.projectNamed", { name: active.project_name })} className="max-w-64 min-w-0">
          <span className="truncate">{active.project_name}</span>
          <span className="hidden text-xs font-normal text-ink-muted sm:inline">{tLoose("roles", active.role)}</span>
          <ChevronsUpDown className="text-ink-faint" aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-64">
        <DropdownMenuLabel>{t("shell.switchProject")}</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={active.project_id} onValueChange={select}>
          {memberships.map((membership) => (
            <DropdownMenuRadioItem key={membership.project_id} value={membership.project_id}>
              <span className="min-w-0 flex-1 truncate">{membership.project_name}</span>
              <span className="text-xs text-ink-muted">{tLoose("roles", membership.role)}</span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

const THEME_OPTIONS = [
  { value: "light", icon: Sun },
  { value: "dark", icon: Moon },
  { value: "system", icon: Monitor },
] as const;

export function ThemeMenu() {
  const { theme, setTheme } = useTheme();
  const { t } = useI18n();
  const current = THEME_OPTIONS.find((option) => option.value === theme) ?? THEME_OPTIONS[2];
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label={t("shell.themeNamed", { name: t(`shell.themes.${current.value}`) })}>
          <current.icon aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>{t("shell.theme")}</DropdownMenuLabel>
        <DropdownMenuRadioGroup
          value={theme}
          onValueChange={(value) => {
            if (isThemePreference(value)) setTheme(value);
          }}
        >
          {THEME_OPTIONS.map((option) => (
            <DropdownMenuRadioItem key={option.value} value={option.value}>
              <option.icon aria-hidden="true" />
              {t(`shell.themes.${option.value}`)}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function initials(operator: Operator): string {
  const words = operator.display_name.trim().split(/\s+/).filter(Boolean);
  const letters = words.length > 1 ? `${words[0]?.[0] ?? ""}${words[words.length - 1]?.[0] ?? ""}` : (words[0] ?? "").slice(0, 2);
  return (letters || operator.email.slice(0, 2)).toUpperCase();
}

/** Where the operator is: console, then the current section. Hidden on narrow screens (the page title says it). */
export function Breadcrumbs() {
  const { pathname } = useLocation();
  const { t } = useI18n();
  const item = NAV_ITEMS.find((candidate) => (candidate.to === "/" ? pathname === "/" : pathname.startsWith(candidate.to)));
  if (!item) return null;
  return (
    <nav aria-label={t("shell.breadcrumb")} className="ml-2 hidden min-w-0 md:block">
      <ol className="flex items-center gap-1.5 text-sm text-ink-muted">
        <li>
          <Link to="/" className="rounded-xs hover:text-ink">
            {t("shell.console")}
          </Link>
        </li>
        <li aria-hidden="true" className="text-ink-faint">
          /
        </li>
        <li aria-current="page" className="truncate font-medium text-ink">
          {t(item.labelKey)}
        </li>
      </ol>
    </nav>
  );
}

export function OperatorMenu({ operator }: { operator: Operator }) {
  const { membership } = useProject();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const [signingOut, setSigningOut] = useState(false);
  const { t, tLoose } = useI18n();

  async function signOut() {
    setSigningOut(true);
    try {
      await logout();
    } catch (error) {
      // A 401 means the session is already gone, which is what the operator wanted.
      if (!isApiError(error) || error.status !== 401) {
        setSigningOut(false);
        toast({
          tone: "danger",
          title: t("shell.signOutFailed"),
          description: isApiError(error) && error.isNetworkError ? `${t("routeError.network")} ${t("common.tryAgain")}` : t("common.tryAgain"),
        });
        return;
      }
    }
    clearSession(queryClient);
    await navigate({ to: "/login", search: {} });
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" aria-label={t("shell.operatorMenu", { name: operator.display_name })} className="px-1.5 sm:px-2.5">
          <span
            aria-hidden="true"
            className="grid size-7 place-items-center rounded-full bg-primary-soft text-xs font-semibold text-primary"
          >
            {initials(operator)}
          </span>
          <span className="hidden max-w-40 truncate sm:inline">{operator.display_name}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-60">
        <div className="px-2 py-1.5">
          <p className="truncate text-sm font-medium">{operator.display_name}</p>
          <p className="truncate text-xs text-ink-muted">{operator.email}</p>
          {membership ? (
            <p className="mt-1.5 flex items-center gap-1.5 text-xs text-ink-muted">
              <Badge tone={roleTone(membership.role)}>{tLoose("roles", membership.role)}</Badge>
              {t("shell.inProject", { project: membership.project_name })}
            </p>
          ) : null}
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuLabel>{t("language.label")}</DropdownMenuLabel>
        <LanguageRadioGroup />
        <DropdownMenuSeparator />
        <DropdownMenuItem disabled={signingOut} onSelect={() => void signOut()}>
          <LogOut aria-hidden="true" />
          {t("shell.signOut")}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
