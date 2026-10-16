import { expect, PERSONAS, test } from "../fixtures";
import { ApiKeysPage } from "../pages/api-keys-page";
import { uniqueName } from "../support/data";

test.describe("API keys", () => {
  test("a viewer cannot create or revoke API keys", async ({ loginAs }) => {
    const page = await loginAs(PERSONAS.diego);
    const keys = new ApiKeysPage(page);
    await keys.goto();

    // Gated controls are shown disabled or hidden, never usable.
    if ((await keys.createButton.count()) > 0) await expect(keys.createButton).toBeDisabled();
    await expect(keys.anyRevokeButton).toHaveCount(0);
  });

  test("an owner creates a key (shown once), then revokes it", async ({ loginAs }) => {
    const page = await loginAs(PERSONAS.ana);
    const keys = new ApiKeysPage(page);
    await keys.goto();
    const name = uniqueName("key");

    await keys.create(name, /^projects:read/);
    await expect(keys.issuedKey).not.toHaveValue("");

    // Once confirmed, the plaintext is gone for good and the table lists the key.
    await keys.confirmStored();
    await expect(keys.row(name)).toBeVisible();
    await keys.goto(); // fresh page load of the app, then back to the screen
    await expect(keys.row(name)).toBeVisible();
    await expect(keys.issuedKey).toHaveCount(0);

    await keys.revoke(name);
    await expect(keys.row(name)).toContainText(/revoked/i);
    await expect(keys.revokeButton(name)).toHaveCount(0);
  });
});
