import { PERSONAS, test } from "../fixtures";

// Journeys for screens still being built. Un-fixme each one when its screen ships and fill in the body.
test.describe("screens still being built", () => {
  test.fixme("an owner invites a member and changes their role", async ({ loginAs }) => {
    // TODO(members screen, /members): invite, change role, remove; a viewer sees no controls.
    await loginAs(PERSONAS.ana);
  });

  test.fixme("an operator creates and edits a customer", async ({ loginAs }) => {
    // TODO(customers screen): not routed yet (/customers is a placeholder); create, search, edit.
    await loginAs(PERSONAS.carla);
  });
});
