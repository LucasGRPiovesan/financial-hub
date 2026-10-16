"""The fictitious records ``seed-demo`` puts in the "Demo Store" project (data only, no I/O).

Everything here is invented: the names are common Brazilian ones, emails use the reserved
``example.com`` domain, and no customer carries a CPF or CNPJ (a customer's document is optional).
Nothing is random. A payment's ``external_ref`` is the per-project key the payments service
enforces, so a second run finds each payment by its key and creates nothing.
"""

from dataclasses import dataclass
from datetime import date
from typing import Final

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
