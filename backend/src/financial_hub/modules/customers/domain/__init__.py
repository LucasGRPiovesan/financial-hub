"""Customers domain (pure). Other modules use ``customers.application.api`` only."""

from financial_hub.modules.customers.domain.documents import (
    CustomerKind,
    Document,
    cnpj_is_valid,
    cpf_is_valid,
    derive_hash_key,
    document_aad,
    document_hash,
    mask_document,
    parse_document,
)
from financial_hub.modules.customers.domain.fields import (
    CustomerIdentity,
    normalize_email,
    normalize_phone,
    validate_external_ref,
    validate_metadata,
    validate_name,
)

__all__ = [
    "CustomerIdentity",
    "CustomerKind",
    "Document",
    "cnpj_is_valid",
    "cpf_is_valid",
    "derive_hash_key",
    "document_aad",
    "document_hash",
    "mask_document",
    "normalize_email",
    "normalize_phone",
    "parse_document",
    "validate_external_ref",
    "validate_metadata",
    "validate_name",
]
