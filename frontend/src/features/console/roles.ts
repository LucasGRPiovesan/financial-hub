import type { BadgeProps } from "../../components/ui/badge";

/** Roles from most to least privileged, as the API names them. */
export const ROLES = ["owner", "admin", "finance", "viewer"] as const;
export type Role = (typeof ROLES)[number];

export function isRole(value: string): value is Role {
  return (ROLES as readonly string[]).includes(value);
}

/** Role badge colour; the role name is always printed, so colour is never the only signal. */
export function roleTone(role: string): NonNullable<BadgeProps["tone"]> {
  switch (role) {
    case "owner":
      return "primary";
    case "admin":
      return "info";
    case "finance":
      return "success";
    default:
      return "neutral";
  }
}
