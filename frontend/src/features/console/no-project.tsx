import { FolderPlus } from "lucide-react";
import { useState } from "react";

import { Button } from "../../components/ui/button";
import { EmptyState } from "../../components/ui/empty-state";
import { CreateProjectDialog } from "./project-dialogs";

/** Shown by project-scoped pages when the operator has no project yet. */
export function NoProjectState() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <EmptyState
        icon={FolderPlus}
        title="No project selected"
        description="Everything here belongs to a project. Create one to get started."
        action={<Button onClick={() => setOpen(true)}>Create project</Button>}
      />
      <CreateProjectDialog open={open} onOpenChange={setOpen} />
    </>
  );
}
