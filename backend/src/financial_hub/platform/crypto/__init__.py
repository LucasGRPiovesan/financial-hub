"""Secrets at rest: AES-256-GCM with a versioned keyring (docs/SECURITY.md).

Deliberately empty: encryption and decryption live in separate modules so that import-linter can
keep decryption out of every layer but a module's infrastructure (``pyproject.toml``, contract
"decryption is importable only by provider infrastructure"). Import what you need from
``keyring``, ``seal`` (encrypt) or ``unseal`` (decrypt).
"""
