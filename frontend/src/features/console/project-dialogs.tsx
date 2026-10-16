import { useState, type FormEvent } from "react";

import type { ProjectOut } from "../../api/generated/model";
import { Field } from "../../components/form-field";
import { Button } from "../../components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { useToast } from "../../components/ui/use-toast";
import { selectProject } from "../../lib/project-selection";
import { useCreateProject, useUpdateProject } from "./api";
import { fieldErrorsOf } from "./errors";

const SLUG_HINT = "Lowercase letters, digits and hyphens. Used in URLs; can't be changed later.";

export function CreateProjectDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <CreateProjectForm onDone={() => onOpenChange(false)} />
      </DialogContent>
    </Dialog>
  );
}

// The form lives inside the content so closing the dialog unmounts it and clears the fields and errors.
function CreateProjectForm({ onDone }: { onDone: () => void }) {
  const toast = useToast();
  const create = useCreateProject();
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const { fields, form } = fieldErrorsOf(create.error);

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate(
      { slug: slug.trim(), name: name.trim() },
      {
        onSuccess: (project) => {
          selectProject(project.id);
          toast({ tone: "success", title: "Project created", description: `${project.name} is now your active project.` });
          onDone();
        },
        onError: () => toast({ tone: "danger", title: "Couldn't create the project" }),
      },
    );
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
      <DialogHeader>
        <DialogTitle>Create project</DialogTitle>
        <DialogDescription>You'll be its owner.</DialogDescription>
      </DialogHeader>
      <Field label="Name" error={fields.name}>
        <Input value={name} onChange={(event) => setName(event.target.value)} autoComplete="off" required />
      </Field>
      <Field label="Slug" error={fields.slug} hint={SLUG_HINT}>
        <Input value={slug} onChange={(event) => setSlug(event.target.value)} autoComplete="off" required />
      </Field>
      {form ? (
        <p role="alert" className="text-sm text-danger">
          {form}
        </p>
      ) : null}
      <DialogFooter>
        <Button type="button" variant="secondary" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" disabled={create.isPending}>
          {create.isPending ? "Creating…" : "Create project"}
        </Button>
      </DialogFooter>
    </form>
  );
}

export function RenameProjectDialog({ project, onClose }: { project: ProjectOut | null; onClose: () => void }) {
  return (
    <Dialog open={project !== null} onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent>{project ? <RenameProjectForm project={project} onDone={onClose} /> : null}</DialogContent>
    </Dialog>
  );
}

function RenameProjectForm({ project, onDone }: { project: ProjectOut; onDone: () => void }) {
  const toast = useToast();
  const update = useUpdateProject(project.id);
  const [name, setName] = useState(project.name);
  const { fields, form } = fieldErrorsOf(update.error);

  function submit(event: FormEvent) {
    event.preventDefault();
    update.mutate(
      { name: name.trim() },
      {
        onSuccess: () => {
          toast({ tone: "success", title: "Project renamed" });
          onDone();
        },
        onError: () => toast({ tone: "danger", title: "Couldn't rename the project" }),
      },
    );
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
      <DialogHeader>
        <DialogTitle>Project settings</DialogTitle>
        <DialogDescription>Rename {project.slug}.</DialogDescription>
      </DialogHeader>
      <Field label="Name" error={fields.name}>
        <Input value={name} onChange={(event) => setName(event.target.value)} autoComplete="off" required />
      </Field>
      {form ? (
        <p role="alert" className="text-sm text-danger">
          {form}
        </p>
      ) : null}
      <DialogFooter>
        <Button type="button" variant="secondary" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" disabled={update.isPending}>
          {update.isPending ? "Saving…" : "Save"}
        </Button>
      </DialogFooter>
    </form>
  );
}
