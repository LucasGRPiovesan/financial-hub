"""Reconciliation reads against the REAL Asaas Sandbox (W8.1). Skipped unless ``FH_ASAAS_API_KEY``.

Read-only: it lists the payments of the last days (``GET /payments`` by ``dateCreated``, every
page) and reads ``GET /finance/balance``, then feeds the listing to the pure matcher. Nothing is
created and the database is never touched. The key is read from the environment and never
printed, logged or asserted into a message.

The sandbox account is shared with other suites and humans, so the test asserts shapes and
invariants, never counts or amounts (docs/RECONCILIATION.md, "shared sandbox").

    docker compose run --rm -e FH_ASAAS_API_KEY test \
        pytest tests/integration/reconciliation/test_asaas_live_reconciliation.py -q --no-cov
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from financial_hub.modules.payments.domain import ProviderRejected
from financial_hub.modules.payments.infrastructure.asaas_provider import AsaasPaymentProvider
from financial_hub.modules.reconciliation.domain.matcher import ExternalRecord, match
from financial_hub.shared.money import BRL

API_KEY = os.environ.get("FH_ASAAS_API_KEY", "")
SAO_PAULO = ZoneInfo("America/Sao_Paulo")

pytestmark = [
    pytest.mark.asaas_live,
    pytest.mark.skipif(not API_KEY, reason="FH_ASAAS_API_KEY is not set"),
]


@pytest.fixture
async def provider() -> AsyncIterator[AsaasPaymentProvider]:
    yield AsaasPaymentProvider(API_KEY)  # the sandbox base URL is the default


async def test_the_sandbox_lists_payments_of_a_window_and_reads_the_balance(
    provider: AsaasPaymentProvider,
) -> None:
    yesterday = datetime.now(UTC).astimezone(SAO_PAULO).date() - timedelta(days=1)
    listed = await provider.list_payments(yesterday - timedelta(days=6), yesterday)
    balance = await provider.get_balance()

    ids = [payment.provider_payment_id for payment in listed]
    assert len(ids) == len(set(ids))  # paging never repeats a payment
    for payment in listed:
        assert payment.provider_payment_id.startswith("pay_")
        assert isinstance(payment.amount.minor_units, int)
        assert payment.amount.currency == BRL
        assert payment.net_amount is None or isinstance(payment.net_amount, int)
        assert all(isinstance(refund.amount.minor_units, int) for refund in payment.refunds)
    assert balance.amount.currency == BRL
    assert isinstance(balance.amount.minor_units, int)

    external = [
        ExternalRecord(
            provider_payment_id=payment.provider_payment_id,
            external_reference=payment.external_reference,
            currency=payment.amount.currency.code,
            amount=payment.amount.minor_units,
            capture_status=payment.capture_status.value,
            settlement_status=payment.settlement_status.value,
            dispute_status=payment.dispute_status.value,
            net_amount=payment.net_amount,
            refunded=0,
        )
        for payment in listed
    ]
    result = match([], external)
    assert result.pairs == []
    assert len(result.external_only) == len(external)  # unknown to an empty hub


async def test_an_inverted_window_is_refused_without_a_call(
    provider: AsaasPaymentProvider,
) -> None:
    today = datetime.now(UTC).astimezone(SAO_PAULO).date()
    with pytest.raises(ProviderRejected):
        await provider.list_payments(today, today - timedelta(days=1))
