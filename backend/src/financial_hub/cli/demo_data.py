"""The fictitious records ``seed-demo`` puts in the "Demo Store" project (data only, no I/O).

Everything here is invented: the names are common Brazilian ones, emails use the reserved
``example.com`` domain, and no customer carries a CPF or CNPJ (a customer's document is optional).
Nothing is random. A payment's ``external_ref`` is the per-project key the payments service
enforces, so a second run finds each payment by its key and creates nothing.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from financial_hub.modules.payments.infrastructure.fake_provider import FakeBehavior

DEMO_CURRENCY: Final = "BRL"
# Fixed, not "today": the seed must describe the same payments whenever it runs.
DUE_DATE_ANCHOR: Final = date(2026, 11, 1)

DEMO_KEY_NAME: Final = "Demo storefront"
DEMO_KEY_SCOPES: Final = ("customers:read", "customers:write", "payments:read", "payments:write")


@dataclass(frozen=True, slots=True)
class DemoCustomer:
    external_ref: str
    name: str
    kind: str  # "individual" or "company"
    email: str
    phone: str


DEMO_CUSTOMERS: Final = (
    DemoCustomer(
        "cust-0001", "Mariana Souza", "individual", "mariana.souza@example.com", "+55 11 90000-0001"
    ),
    DemoCustomer(
        "cust-0002",
        "Lucas Ferreira",
        "individual",
        "lucas.ferreira@example.com",
        "+55 21 90000-0002",
    ),
    DemoCustomer(
        "cust-0003",
        "Juliana Almeida",
        "individual",
        "juliana.almeida@example.com",
        "+55 31 90000-0003",
    ),
    DemoCustomer(
        "cust-0004",
        "Rafael Ribeiro",
        "individual",
        "rafael.ribeiro@example.com",
        "+55 41 90000-0004",
    ),
    DemoCustomer(
        "cust-0005",
        "Padaria Estrela Ltda",
        "company",
        "financeiro.estrela@example.com",
        "+55 11 3000-0005",
    ),
    DemoCustomer(
        "cust-0006",
        "Camila Nogueira",
        "individual",
        "camila.nogueira@example.com",
        "+55 51 90000-0006",
    ),
    DemoCustomer(
        "cust-0007",
        "Thiago Barbosa",
        "individual",
        "thiago.barbosa@example.com",
        "+55 71 90000-0007",
    ),
    DemoCustomer(
        "cust-0008",
        "Loja Verde Comercio ME",
        "company",
        "contas.verde@example.com",
        "+55 85 3000-0008",
    ),
)


@dataclass(frozen=True, slots=True)
class DemoPayment:
    """One payment and what happens to it after creation, all through the fake provider.

    ``behavior`` is how the fake provider answers the create call: ``SUCCEED`` leaves it pending,
    ``SUCCEED_CAPTURED`` captures it at once, ``REJECT`` refuses it. ``settle`` settles a captured
    payment at the provider; the refund fields refund a captured payment through the service.
    """

    external_ref: str
    customer: str
    method: str  # "pix", "boleto" or "card"
    amount: int  # minor units (centavos)
    due_in_days: int
    behavior: FakeBehavior
    settle: bool = False
    partial_refund: int | None = None
    full_refund: bool = False


DEMO_PAYMENTS: Final = (
    DemoPayment(
        "demo-pay-01", "cust-0001", "pix", 4990, 3, FakeBehavior.SUCCEED_CAPTURED, settle=True
    ),
    DemoPayment(
        "demo-pay-02", "cust-0002", "pix", 12990, 5, FakeBehavior.SUCCEED_CAPTURED, settle=True
    ),
    DemoPayment("demo-pay-03", "cust-0003", "pix", 8900, 7, FakeBehavior.SUCCEED),
    DemoPayment("demo-pay-04", "cust-0004", "pix", 2750, 10, FakeBehavior.SUCCEED),
    DemoPayment("demo-pay-05", "cust-0005", "boleto", 129900, 15, FakeBehavior.SUCCEED),
    DemoPayment("demo-pay-06", "cust-0006", "boleto", 45000, 12, FakeBehavior.SUCCEED_CAPTURED),
    DemoPayment("demo-pay-07", "cust-0007", "boleto", 19900, 9, FakeBehavior.REJECT),
    DemoPayment(
        "demo-pay-08", "cust-0008", "card", 7990, 4, FakeBehavior.SUCCEED_CAPTURED, settle=True
    ),
    DemoPayment(
        "demo-pay-09",
        "cust-0001",
        "card",
        24900,
        6,
        FakeBehavior.SUCCEED_CAPTURED,
        partial_refund=5000,
    ),
    DemoPayment(
        "demo-pay-10", "cust-0002", "card", 9990, 8, FakeBehavior.SUCCEED_CAPTURED, full_refund=True
    ),
    DemoPayment("demo-pay-11", "cust-0003", "card", 14900, 11, FakeBehavior.REJECT),
    DemoPayment("demo-pay-12", "cust-0005", "card", 33000, 14, FakeBehavior.SUCCEED),
)

# Billing (W5.3): a monthly fee plus API calls at BRL 0.05 each, on one plan. The first
# subscription starts ``DEMO_BILLED_SUBSCRIPTION_AGE_DAYS`` before the seed runs, so its first
# period has closed into one finalized invoice; the second is still in its trial.
DEMO_METRIC_CODE: Final = "api_calls"
DEMO_METRIC_NAME: Final = "Chamadas de API"
DEMO_FEE_PRICE: Final = "demo-monthly-fee"
DEMO_USAGE_PRICE: Final = "demo-api-calls"
DEMO_PRICE_EFFECTIVE_FROM: Final = date(2026, 1, 1)
DEMO_FEE_MODEL: Final = {"kind": "flat_fee", "amount": 4990}
DEMO_USAGE_MODEL: Final = {"kind": "per_unit", "unit_price": "0.05"}
DEMO_PLAN_CODE: Final = "demo-pro"
DEMO_PLAN_NAME: Final = "Demo Pro"
DEMO_BILLED_CUSTOMER: Final = "cust-0001"
DEMO_TRIAL_CUSTOMER: Final = "cust-0002"
DEMO_BILLED_SUBSCRIPTION_AGE_DAYS: Final = 35
DEMO_TRIAL_DAYS: Final = 14
DEMO_METRIC_FIELD: Final = "calls"
# (days before the seed runs, calls): inside the closed first period and the 30-day usage window;
# 1500 calls x BRL 0.05 = BRL 75.00, so the invoice is 4990 + 7500 = 12490 minor units
DEMO_USAGE: Final = (
    (28, 120),
    (24, 340),
    (20, 95),
    (17, 210),
    (14, 180),
    (12, 255),
    (10, 140),
    (8, 160),
)

# Integration manifest (W6.1): v1 is active (authored by Ana, approved and activated by Carla:
# four-eyes, it sets a split); v2 is a draft that moves the clinic's split from 70% to 65%.
DEMO_MANIFEST_EVENT: Final = "consultation.completed"
DEMO_MANIFEST_V1: Final[dict[str, Any]] = {
    "schema_version": "manifest.v1",
    "description": "Consultas da Demo Store: cada consulta concluída registra as chamadas de API.",
    "product": {"code": "demo-store", "name": "Demo Store"},
    "customers": {"external_id": "/customer/id", "name": "/customer/name"},
    "business_events": [
        {
            "name": DEMO_MANIFEST_EVENT,
            "payload_schema": {
                "type": "object",
                "required": ["customer", "consultation_id", "calls"],
                "properties": {
                    "customer": {
                        "type": "object",
                        "required": ["id"],
                        "properties": {
                            "id": {"type": "string", "maxLength": 128},
                            "name": {"type": "string", "maxLength": 200},
                        },
                    },
                    "consultation_id": {"type": "string", "maxLength": 64},
                    "calls": {"type": "integer", "minimum": 1, "maximum": 10000},
                },
            },
            "usage": [
                {"metric": DEMO_METRIC_CODE, "quantity": {"op": "to_decimal", "of": "/calls"}}
            ],
        }
    ],
    "metrics": [{"source": "existing", "code": DEMO_METRIC_CODE}],
    "prices": [{"code": DEMO_USAGE_PRICE}],
    "splits": [{"payee": "clinica-parceira", "basis_points": 7000}],
    "wallet": {"mode": "disabled"},
    "webhooks": [],
}
DEMO_MANIFEST_V2: Final[dict[str, Any]] = {
    **DEMO_MANIFEST_V1,
    "description": "Rascunho: repasse da clínica parceira de 70% para 65%.",
    "splits": [{"payee": "clinica-parceira", "basis_points": 6500}],
}
