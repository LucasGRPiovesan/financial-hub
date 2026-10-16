"""Standard Webhooks signing (https://www.standardwebhooks.com/): known vector, overlap, secrets."""

import base64

import pytest

from financial_hub.modules.webhooks.domain import (
    SECRET_PREFIX,
    InvalidWebhookSecretError,
    generate_secret,
    secret_key,
    sign,
    signature_header,
    verify,
)

# The example of the Standard Webhooks specification and its reference libraries.
SPEC_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
SPEC_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
SPEC_TIMESTAMP = 1614265330
SPEC_BODY = b'{"test": 2432232314}'
SPEC_SIGNATURE = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def test_known_vector_of_the_specification() -> None:
    signature = sign(SPEC_SECRET, message_id=SPEC_ID, timestamp=SPEC_TIMESTAMP, body=SPEC_BODY)

    assert signature == SPEC_SIGNATURE


def test_verify_accepts_the_vector_and_rejects_tampering() -> None:
    ts = SPEC_TIMESTAMP

    assert verify(SPEC_SECRET, SPEC_SIGNATURE, message_id=SPEC_ID, timestamp=ts, body=SPEC_BODY)
    assert not verify(
        SPEC_SECRET, SPEC_SIGNATURE, message_id=SPEC_ID, timestamp=ts, body=b'{"test": 1}'
    )
    assert not verify(
        SPEC_SECRET,
        SPEC_SIGNATURE,
        message_id=SPEC_ID,
        timestamp=SPEC_TIMESTAMP + 1,
        body=SPEC_BODY,
    )


def test_rotation_overlap_header_carries_both_signatures_space_separated() -> None:
    old, new = generate_secret(), generate_secret()
    msg, ts, body = "evt_x", 1_700_000_000, b"{}"

    header = signature_header([new, old], message_id=msg, timestamp=ts, body=body)

    first, second = header.split(" ")
    assert first == sign(new, message_id=msg, timestamp=ts, body=body) and second == sign(
        old, message_id=msg, timestamp=ts, body=body
    )
    assert verify(new, header, message_id=msg, timestamp=ts, body=body) and verify(
        old, header, message_id=msg, timestamp=ts, body=body
    )
    assert not verify(generate_secret(), header, message_id=msg, timestamp=ts, body=body)


def test_generated_secret_is_whsec_plus_32_random_bytes() -> None:
    secret = generate_secret()

    assert secret.startswith(SECRET_PREFIX)
    assert len(base64.b64decode(secret.removeprefix(SECRET_PREFIX))) == 32
    assert secret_key(secret) == base64.b64decode(secret.removeprefix(SECRET_PREFIX))
    assert generate_secret() != secret


@pytest.mark.parametrize(
    "value", ["MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw", "whsec_", "whsec_not base64!"]
)
def test_malformed_secrets_are_rejected(value: str) -> None:
    with pytest.raises(InvalidWebhookSecretError):
        secret_key(value)
