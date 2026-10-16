import { describe, expect, it } from "vitest";

import type { AuditRecordOut } from "../../api/generated/model";
import { describeActivity, humanizeAction } from "./audit-activity";

const NAMES = new Map([
  ["opr_ana", "Ana Owner"],
  ["opr_bruno", "Bruno Admin"],
]);

function record(overrides: Partial<AuditRecordOut>): AuditRecordOut {
  return {
    id: "aud_1",
    action: "member.added",
    actor_type: "operator",
    actor_id: "opr_ana",
    target_type: "operator",
    target_id: "opr_bruno",
    diff: null,
    project_id: "prj_1",
    occurred_at: "2026-10-03T09:00:00Z",
    ip: null,
    user_agent: null,
    request_id: null,
    correlation_id: null,
    ...overrides,
  };
}

describe("describeActivity sentences", () => {
  it.each<[string, Partial<AuditRecordOut>, string]>([
    ["member added with role", { diff: { after: { role: "admin" } } }, "Bruno Admin was added as admin"],
    ["member added without data", { target_id: "opr_unknown", diff: {} }, "Member added"],
    ["member added, unknown operator", { target_id: "opr_unknown", diff: { after: { role: "viewer" } } }, "A member was added as viewer"],
    [
      "role changed",
      { action: "member.role_changed", diff: { before: { role: "viewer" }, after: { role: "finance" } } },
      "Bruno Admin is now finance (was viewer)",
    ],
    ["role changed without data", { action: "member.role_changed" }, "Member role changed"],
    ["member removed", { action: "member.removed" }, "Bruno Admin was removed from the project"],
    ["member removed, unknown", { action: "member.removed", target_id: "opr_zzz" }, "Member removed"],
    ["customer created", { action: "customer.created", diff: { after: { name: "Marina Alves" } } }, 'Customer "Marina Alves" was created'],
    ["customer created, no name", { action: "customer.created" }, "Customer created"],
    ["customer updated", { action: "customer.updated", diff: { before: { name: "M" }, after: { name: "Marina" } } }, 'Customer "Marina" was updated'],
    ["customer anonymized", { action: "customer.anonymized" }, "Customer anonymized"],
    ["api key created", { action: "api_key.created", diff: { after: { name: "Checkout" } } }, 'API key "Checkout" was created'],
    ["api key revoked", { action: "api_key.revoked", diff: { before: { name: "Checkout" }, after: { name: "Checkout" } } }, 'API key "Checkout" was revoked'],
    ["api key revoked, no name", { action: "api_key.revoked" }, "API key revoked"],
    ["project created", { action: "project.created", diff: { after: { name: "Demo Store" } } }, 'Project "Demo Store" was created'],
    ["provider account added", { action: "provider_account.created" }, "Provider account added"],
    ["provider account disabled", { action: "provider_account.disabled" }, "Provider account disabled"],
    ["provider account unknown verb", { action: "provider_account.rotated" }, "Provider account updated"],
    ["unknown action", { action: "project.updated" }, "Project updated"],
    ["non-string diff values are ignored", { action: "customer.created", diff: { after: { name: 42 } } }, "Customer created"],
  ])("%s", (_label, overrides, sentence) => {
    expect(describeActivity(record(overrides), NAMES).sentence).toBe(sentence);
  });
});

describe("describeActivity actor and ids", () => {
  it("names a known operator and lists target then actor ids", () => {
    const line = describeActivity(record({}), NAMES);
    expect(line.actor).toBe("Ana Owner");
    expect(line.ids).toEqual(["opr_bruno", "opr_ana"]);
  });

  it.each([
    [{ actor_type: "operator", actor_id: "opr_x" }, "an operator"],
    [{ actor_type: "api_key", actor_id: "key_1" }, "an API key"],
    [{ actor_type: "system", actor_id: null }, "the system"],
  ] as const)("falls back to a generic actor for %j", (overrides, actor) => {
    expect(describeActivity(record(overrides), NAMES).actor).toBe(actor);
  });

  it("leaves the system actor out of the id line", () => {
    expect(describeActivity(record({ actor_type: "system", actor_id: "sys", target_id: null }), NAMES).ids).toEqual([]);
  });
});

describe("humanizeAction", () => {
  it.each([
    ["member.added", "Member added"],
    ["provider_account.status_changed", "Provider account status changed"],
    ["", "Activity recorded"],
  ])("%j -> %j", (action, text) => {
    expect(humanizeAction(action)).toBe(text);
  });
});
