import { useState, type FormEvent } from "react";

import { Field } from "../../components/form-field";
import { Button } from "../../components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import { Select } from "../../components/ui/select";
import { useToast } from "../../components/ui/use-toast";
import { useI18n } from "../../i18n";
import { fieldErrorsOf } from "./errors";
import { useAddMember } from "./members-api";
import { isRole, ROLES, type Role } from "./roles";

export function AddMemberDialog({
  open,
  onOpenChange,
  projectId,
  mayGrantOwner,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectId: string;
  mayGrantOwner: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <AddMemberForm projectId={projectId} mayGrantOwner={mayGrantOwner} onDone={() => onOpenChange(false)} />
      </DialogContent>
    </Dialog>
  );
}

// The form lives inside the content so closing the dialog unmounts it and clears the fields and errors.
function AddMemberForm({ projectId, mayGrantOwner, onDone }: { projectId: string; mayGrantOwner: boolean; onDone: () => void }) {
  const toast = useToast();
  const { t, tLoose } = useI18n();
  const add = useAddMember(projectId);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const { fields, form } = fieldErrorsOf(add.error);

  function submit(event: FormEvent) {
    event.preventDefault();
    add.mutate(
      { email: email.trim(), role },
      {
        onSuccess: (member) => {
          toast({ tone: "success", title: t("members.added"), description: t("members.roleChangedDescription", { name: member.display_name, role: tLoose("roles", member.role) }) });
          onDone();
        },
        onError: (error) => {
          // Field problems (unknown email, already a member) stay on the form; a 403 or anything else is also a toast.
          const { form: message } = fieldErrorsOf(error);
          if (message !== null) toast({ tone: "danger", title: t("members.addFailed"), description: message });
        },
      },
    );
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4" noValidate>
      <DialogHeader>
        <DialogTitle>{t("members.add")}</DialogTitle>
        <DialogDescription>{t("members.addDescription")}</DialogDescription>
      </DialogHeader>
      <Field label={t("members.emailLabel")} error={fields.email}>
        <Input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="off" required />
      </Field>
      <Field label={t("members.roleLabel")} error={fields.role} hint={t(`roles.summary.${role}`)}>
        <Select
          value={role}
          onChange={(event) => {
            if (isRole(event.target.value)) setRole(event.target.value);
          }}
        >
          {ROLES.map((item) => (
            <option key={item} value={item} disabled={item === "owner" && !mayGrantOwner}>
              {t(`roles.${item}`)}
              {item === "owner" && !mayGrantOwner ? ` ${t("members.ownersOnly")}` : ""}
            </option>
          ))}
        </Select>
      </Field>
      {form ? (
        <p role="alert" className="text-sm text-danger">
          {form}
        </p>
      ) : null}
      <DialogFooter>
        <Button type="button" variant="secondary" onClick={onDone}>
          {t("common.cancel")}
        </Button>
        <Button type="submit" disabled={add.isPending || email.trim() === ""}>
          {add.isPending ? t("members.adding") : t("members.add")}
        </Button>
      </DialogFooter>
    </form>
  );
}
