import { FolderPlus } from "lucide-react";
import { useState } from "react";

import { Button } from "../../components/ui/button";
import { EmptyState } from "../../components/ui/empty-state";
import { useT } from "../../i18n";
import { CreateProjectDialog } from "./project-dialogs";

/** Shown by project-scoped pages when the operator has no project yet. */
export function NoProjectState() {
  const [open, setOpen] = useState(false);
  const t = useT();
  return (
    <>
      <EmptyState
        icon={FolderPlus}
        title={t("noProject.title")}
        description={t("noProject.description")}
        action={<Button onClick={() => setOpen(true)}>{t("projects.create")}</Button>}
      />
      <CreateProjectDialog open={open} onOpenChange={setOpen} />
    </>
  );
}
