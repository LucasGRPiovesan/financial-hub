import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { ChevronsUpDown, LogOut, Monitor, Moon, Sun } from "lucide-react";
import { useState } from "react";

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
import { isApiError } from "../../lib/api";
import { isThemePreference, useTheme } from "../../lib/theme";
import { useActiveMembership } from "../../lib/project-selection";
import { logout, type Membership, type Operator } from "../auth/auth-api";
import { clearSession } from "../auth/session";

export function ProjectSwitcher({ memberships }: { memberships: readonly Membership[] }) {
  const { active, select } = useActiveMembership(memberships);

  if (!active) {
    return (
      <Button variant="secondary" disabled aria-label="Project">
        No projects
      </Button>
    );
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="secondary" aria-label={`Project: ${active.project_name}`} className="max-w-64 min-w-0">
          <span className="truncate">{active.project_name}</span>
          <span className="hidden text-xs font-normal text-ink-muted sm:inline">{active.role}</span>
          <ChevronsUpDown className="text-ink-faint" aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-64">
        <DropdownMenuLabel>Switch project</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={active.project_id} onValueChange={select}>
          {memberships.map((membership) => (
            <DropdownMenuRadioItem key={membership.project_id} value={membership.project_id}>
              <span className="min-w-0 flex-1 truncate">{membership.project_name}</span>
              <span className="text-xs text-ink-muted">{membership.role}</span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

const THEME_OPTIONS = [
  { value: "light", label: "Light", icon: Sun },
  { value: "dark", label: "Dark", icon: Moon },
  { value: "system", label: "System", icon: Monitor },
] as const;

export function ThemeMenu() {
  const { theme, setTheme } = useTheme();
  const current = THEME_OPTIONS.find((option) => option.value === theme) ?? THEME_OPTIONS[2];
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label={`Theme: ${current.label}`}>
          <current.icon aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>Theme</DropdownMenuLabel>
        <DropdownMenuRadioGroup
          value={theme}
          onValueChange={(value) => {
            if (isThemePreference(value)) setTheme(value);
          }}
        >
          {THEME_OPTIONS.map((option) => (
            <DropdownMenuRadioItem key={option.value} value={option.value}>
              <option.icon aria-hidden="true" />
              {option.label}
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

export function OperatorMenu({ operator }: { operator: Operator }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const [signingOut, setSigningOut] = useState(false);

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
          title: "Couldn't sign out",
          description: isApiError(error) && error.isNetworkError ? "The API could not be reached. Try again." : "Try again.",
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
        <Button variant="ghost" aria-label={`Operator menu: ${operator.display_name}`} className="px-1.5 sm:px-2.5">
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
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem disabled={signingOut} onSelect={() => void signOut()}>
          <LogOut aria-hidden="true" />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
