"""Smoke test against the REAL Asaas Sandbox (PRO-41). Skipped unless ``FH_ASAAS_API_KEY`` is set.

Run by a human with a sandbox key; the key is read from the environment and never printed, logged
or asserted into a message. It creates one customer and one boleto, looks the charge up by our
external reference, reads it back and cancels it.
It runs in the `test` container but never touches the database.

    docker compose run --rm -e FH_ASAAS_API_KEY test \
        pytest tests/integration/payments/test_asaas_live.py -m asaas_live -q --no-cov
"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import date, timedelta

import pytest

from financial_hub.modules.payments.domain import (
    CaptureStatus,
    CreatePaymentRequest,
    PaymentMethod,
    ProviderCustomerRequest,
    ProviderPayment,
    ProviderUncertain,
)
from financial_hub.modules.payments.infrastructure.asaas_provider import AsaasPaymentProvider
from financial_hub.shared.ids import IdPrefix, new_id
from financial_hub.shared.money import BRL, Money

API_KEY = os.environ.get("FH_ASAAS_API_KEY", "")
# A checksum-valid test CPF (the same one Asaas' own sandbox documentation uses).
TEST_CPF = "24971563792"

pytestmark = [
    pytest.mark.asaas_live,
    pytest.mark.skipif(not API_KEY, reason="FH_ASAAS_API_KEY is not set"),
]


@pytest.fixture
async def provider() -> AsyncIterator[AsaasPaymentProvider]:
    yield AsaasPaymentProvider(API_KEY)  # the sandbox base URL is the default


async def cancel_converging(provider: AsaasPaymentProvider, payment_id: str) -> ProviderPayment:
    """Cancel the way the resolver would: an uncertain answer is never assumed, it is re-read.

    The sandbox answers a DELETE issued right after a boleto is created with HTTP 500 and does not
    cancel it (observed 2026-10-16); the same call succeeds seconds later.
    """
    for attempt in range(5):
        try:
            return await provider.cancel(payment_id)
        except ProviderUncertain:
            current = await provider.get_payment(payment_id)
            if current.capture_status is CaptureStatus.CANCELED:
                return current
            await asyncio.sleep(2 * (attempt + 1))
    return await provider.cancel(payment_id)


async def test_customer_boleto_lookup_read_and_cancel(provider: AsaasPaymentProvider) -> None:
    customer_ref = new_id(IdPrefix.CUSTOMER)
    payment_ref = new_id(IdPrefix.PAYMENT_INTENT)

    customer = await provider.create_customer(
        ProviderCustomerRequest(external_reference=customer_ref, name="Hub Smoke Test"),
        cpf_cnpj=TEST_CPF,
    )
    again = await provider.create_customer(
        ProviderCustomerRequest(external_reference=customer_ref, name="Hub Smoke Test"),
        cpf_cnpj=TEST_CPF,
    )

    created = await provider.create_payment(
        CreatePaymentRequest(
            external_reference=payment_ref,
            amount=Money(12345, BRL),
            method=PaymentMethod.BOLETO,
            due_date=date.today() + timedelta(days=7),
            customer_reference=customer_ref,
            provider_customer_id=customer.provider_customer_id,
            description="financial-hub smoke test",
        )
    )
    found = await provider.lookup_by_external_reference(payment_ref)
    fetched = await provider.get_payment(created.provider_payment_id)
    canceled = await cancel_converging(provider, created.provider_payment_id)

    assert again.provider_customer_id == customer.provider_customer_id  # found, not duplicated
    assert created.external_reference == payment_ref
    assert created.amount == Money(12345, BRL)
    assert created.capture_status is CaptureStatus.PENDING
    assert found is not None and found.provider_payment_id == created.provider_payment_id
    assert fetched.provider_payment_id == created.provider_payment_id
    assert canceled.capture_status is CaptureStatus.CANCELED
