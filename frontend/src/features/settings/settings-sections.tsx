import { Monitor, Moon, Sun, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { version } from "../../../package.json";
import { Badge } from "../../components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../../components/ui/card";
import { useI18n, useLocale } from "../../i18n";
import { cn } from "../../lib/cn";
import { LOCALES, type Locale } from "../../lib/locale";
import { isThemePreference, useTheme, type ThemePreference } from "../../lib/theme";
import type { Membership, Operator } from "../auth/auth-api";
import { roleTone } from "../console/roles";
import { CopyableValue } from "../events/event-parts";

const REPOSITORY_URL = "https://github.com/LucasGRPiovesan/financial-hub";

const LOCALE_NAMES: Record<Locale, string> = { "pt-BR": "Português", "en-US": "English" };
const THEMES: readonly { value: ThemePreference; icon: LucideIcon }[] = [
  { value: "light", icon: Sun },
  { value: "dark", icon: Moon },
  { value: "system", icon: Monitor },
];

function Section({ title, description, children }: { title: string; description: string; children: ReactNode }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid gap-1 py-2.5 sm:grid-cols-[12rem_1fr] sm:items-center sm:gap-4">
      <dt className="text-sm text-ink-muted">{label}</dt>
      <dd className="min-w-0 text-sm">{children}</dd>
    </div>
  );
}

function Rows({ children }: { children: ReactNode }) {
  return <dl className="divide-y divide-line">{children}</dl>;
}

export function ProfileSection({ operator, membership }: { operator: Operator; membership: Membership | null }) {
  const { t, tLoose } = useI18n();
  return (
    <Section title={t("settings.profile.title")} description={t("settings.profile.description")}>
      <Rows>
        <Row label={t("settings.profile.name")}>{operator.display_name}</Row>
        <Row label={t("settings.profile.email")}>
          <span className="break-all">{operator.email}</span>
        </Row>
        <Row label={t("settings.profile.role")}>
          {membership ? <Badge tone={roleTone(membership.role)}>{tLoose("roles", membership.role)}</Badge> : <span className="text-ink-faint">{t("settings.profile.none")}</span>}
        </Row>
      </Rows>
    </Section>
  );
}

function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: readonly { value: T; label: string; icon?: LucideIcon; lang?: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-md border border-line bg-surface-sunken p-0.5">
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={selected}
            lang={option.lang}
            onClick={() => onChange(option.value)}
            className={cn(
              "inline-flex h-8 items-center gap-1.5 rounded-sm px-3 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary",
              selected ? "bg-surface text-ink shadow-sm" : "text-ink-muted hover:text-ink",
            )}
          >
            {option.icon ? <option.icon className="size-4" aria-hidden="true" /> : null}
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

export function PreferencesSection() {
  const { t } = useI18n();
  const { locale, setLocale } = useLocale();
  const { theme, setTheme } = useTheme();
  return (
    <Section title={t("settings.preferences.title")} description={t("settings.preferences.description")}>
      <Rows>
        <Row label={t("settings.preferences.language")}>
          <Segmented
            label={t("settings.preferences.language")}
            value={locale}
            options={LOCALES.map((option) => ({ value: option, label: LOCALE_NAMES[option], lang: option }))}
            onChange={setLocale}
          />
        </Row>
        <Row label={t("settings.preferences.theme")}>
          <Segmented
            label={t("settings.preferences.theme")}
            value={theme}
            options={THEMES.map((option) => ({ value: option.value, label: t(`shell.themes.${option.value}`), icon: option.icon }))}
            onChange={(next) => {
              if (isThemePreference(next)) setTheme(next);
            }}
          />
        </Row>
      </Rows>
    </Section>
  );
}

export function ProjectSection({ membership }: { membership: Membership | null }) {
  const { t } = useI18n();
  return (
    <Section title={t("settings.project.title")} description={t("settings.project.description")}>
      {membership ? (
        <Rows>
          <Row label={t("settings.project.name")}>{membership.project_name}</Row>
          <Row label={t("settings.project.id")}>
            <CopyableValue value={membership.project_id} label={t("settings.project.id")} truncate={false} />
          </Row>
          <Row label={t("settings.project.environment")}>
            <span className="flex flex-wrap items-center gap-2">
              <Badge tone="warning">{t("settings.project.sandbox")}</Badge>
              <span className="text-xs text-ink-muted">{t("settings.project.environmentNote")}</span>
            </span>
          </Row>
        </Rows>
      ) : (
        <p className="text-sm text-ink-muted">{t("settings.noProjectProfile")}</p>
      )}
    </Section>
  );
}

export function AboutSection() {
  const { t } = useI18n();
  const link = "rounded-xs text-primary underline-offset-2 hover:underline";
  return (
    <Section title={t("settings.about.title")} description={t("settings.about.description")}>
      <Rows>
        <Row label={t("settings.about.version")}>
          <span className="figures font-mono text-xs">v{version}</span>
        </Row>
        <Row label={t("settings.about.readme")}>
          <a className={link} href={`${REPOSITORY_URL}#readme`} target="_blank" rel="noreferrer">
            README
          </a>
        </Row>
        <Row label={t("settings.about.docs")}>
          <a className={link} href={`${REPOSITORY_URL}/tree/main/docs`} target="_blank" rel="noreferrer">
            docs/
          </a>
        </Row>
      </Rows>
    </Section>
  );
}
