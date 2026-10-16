import { describe, expect, it } from "vitest";

import { enUS } from "../../i18n/en-US";
import { ptBR } from "../../i18n/pt-BR";
import { PAYMENT_METHODS, PAYMENT_STATUSES, refundTone, secondaryStates, statusKey, statusTone } from "./payment-status";

describe("status mapping", () => {
  it("gives every API status a dictionary entry in both languages", () => {
    for (const status of PAYMENT_STATUSES) {
      const key = statusKey(status) as keyof typeof enUS.payments.status;
      expect(enUS.payments.status[key], status).toBeTruthy();
      expect(ptBR.payments.status[key], status).toBeTruthy();
    }
    for (const method of PAYMENT_METHODS) {
      expect(enUS.payments.method[method]).toBeTruthy();
      expect(ptBR.payments.method[method]).toBeTruthy();
    }
  });

  it("uses Brazilian finance vocabulary in pt-BR", () => {
    expect(ptBR.payments.status.settled).toBe("Liquidado");
    expect(ptBR.payments.status.failed).toBe("Recusado");
    expect(ptBR.payments.status.requiresPayment).toBe("Pendente");
  });

  it("maps statuses to tones and falls back to neutral", () => {
    expect(statusTone("settled")).toBe("success");
    expect(statusTone("failed")).toBe("danger");
    expect(statusTone("charged_back")).toBe("danger");
    expect(statusTone("partially_refunded")).toBe("warning");
    expect(statusTone("something_new")).toBe("neutral");
    expect(refundTone("done")).toBe("success");
    expect(refundTone("denied")).toBe("danger");
    expect(refundTone("submitted")).toBe("info");
  });

  it("lists the orthogonal states that carry information", () => {
    const base = { capture_status: "captured", settlement_status: "none", refund_status: "none", dispute_status: "none" };
    expect(secondaryStates(base)).toEqual([{ field: "capture", value: "captured" }]);
    expect(secondaryStates({ ...base, settlement_status: "settled", refund_status: "partial" })).toEqual([
      { field: "capture", value: "captured" },
      { field: "settlement", value: "settled" },
      { field: "refund", value: "partial" },
    ]);
  });
});
