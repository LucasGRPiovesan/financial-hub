import { PageHeader } from "../components/page-header";
import { useSession } from "../features/auth/use-session";
import { useProject } from "../features/console/use-project";
import { AboutSection, PreferencesSection, ProfileSection, ProjectSection } from "../features/settings/settings-sections";
import { useDocumentTitle } from "../features/shell/document-title";
import { useI18n } from "../i18n";

export function SettingsPage() {
  const { t } = useI18n();
  const { membership } = useProject();
  const { operator } = useSession();
  useDocumentTitle(t("settings.title"));
  return (
    <>
      <PageHeader title={t("settings.title")} description={t("settings.description")} />
      <div className="grid max-w-3xl gap-4">
        <ProfileSection operator={operator} membership={membership} />
        <PreferencesSection />
        <ProjectSection membership={membership} />
        <AboutSection />
      </div>
    </>
  );
}
