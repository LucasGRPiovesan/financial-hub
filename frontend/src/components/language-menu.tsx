import { Globe } from "lucide-react";

import { useI18n, useLocale } from "../i18n";
import { isLocale, LOCALES, type Locale } from "../lib/locale";
import { Button } from "./ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";

/** Each language is named in itself, in both interfaces, so it can always be found. */
const LOCALE_NAMES: Record<Locale, string> = { "pt-BR": "Português", "en-US": "English" };

/** The language choice as a radio group; shared by the standalone menu and the operator menu. */
export function LanguageRadioGroup() {
  const { locale, setLocale } = useLocale();
  const { t } = useI18n();
  return (
    <DropdownMenuRadioGroup
      value={locale}
      aria-label={t("language.menuLabel", { name: LOCALE_NAMES[locale] })}
      onValueChange={(value) => {
        if (isLocale(value)) setLocale(value);
      }}
    >
      {LOCALES.map((option) => (
        <DropdownMenuRadioItem key={option} value={option} lang={option}>
          {LOCALE_NAMES[option]}
        </DropdownMenuRadioItem>
      ))}
    </DropdownMenuRadioGroup>
  );
}

/** Compact "Idioma: Português" control (sign-in page): switches instantly, no reload. */
export function LanguageMenu({ className }: { className?: string }) {
  const { locale, t } = useI18n();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="sm" className={className} aria-label={t("language.menuLabel", { name: LOCALE_NAMES[locale] })}>
          <Globe aria-hidden="true" />
          <span aria-hidden="true">{LOCALE_NAMES[locale]}</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>{t("language.label")}</DropdownMenuLabel>
        <LanguageRadioGroup />
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
