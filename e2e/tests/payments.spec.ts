import { expect, PERSONAS, test } from "../fixtures";
import { PaymentsPage } from "../pages/payments-page";

test.describe("Payments", () => {
  test("an owner browses the seeded payments, opens a settled one and refunds part of it", async ({ loginAs }) => {
    const page = await loginAs(PERSONAS.ana);
    const payments = new PaymentsPage(page);
    await payments.goto();

    // The seed has PIX, boleto and card charges in several states.
    await expect(payments.table).toBeVisible();
    await expect(payments.row("demo-pay-02")).toContainText("Pix");
    await expect(payments.row("demo-pay-02")).toContainText("Liquidado");
    await expect(payments.row("demo-pay-07")).toContainText("Recusado");
    await expect(payments.row("demo-pay-10")).toContainText("Estornado");

    await payments.open("demo-pay-02");
    await expect(page.getByText("Nenhum estorno solicitado.")).toBeVisible();

    await payments.refundPartially("10,00", "Cliente desistiu");

    // The refund shows up in the refunds table and the status follows the API.
    const refunds = page.getByRole("table", { name: "Estornos" });
    await expect(refunds).toContainText("R$ 10,00");
    await expect(refunds).toContainText("Cliente desistiu");
    await expect(page.getByText("Nenhum estorno solicitado.")).toHaveCount(0);
  });

  test("a viewer sees the payment but cannot refund it", async ({ loginAs }) => {
    const page = await loginAs(PERSONAS.diego);
    const payments = new PaymentsPage(page);
    await payments.goto();
    await payments.open("demo-pay-08");
    await expect(payments.refundButton).toBeDisabled();
  });
});
