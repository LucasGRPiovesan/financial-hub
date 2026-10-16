"""Valid CPF/CNPJ generators for tests (deterministic from a seed; never real people's numbers)."""

import random

CNPJ_FIRST_WEIGHTS = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
CNPJ_SECOND_WEIGHTS = (6, *CNPJ_FIRST_WEIGHTS)


def _cpf_check(digits: list[int]) -> int:
    weight = len(digits) + 1
    return sum(d * (weight - i) for i, d in enumerate(digits)) * 10 % 11 % 10


def make_cpf(seed: int | None = None) -> str:
    rng = random.Random(seed)  # noqa: S311  (test data, not a secret)
    base = [rng.randrange(10) for _ in range(9)]
    if len(set(base)) == 1:
        base[0] = (base[0] + 1) % 10
    first = _cpf_check(base)
    second = _cpf_check([*base, first])
    return "".join(map(str, [*base, first, second]))


def _cnpj_check(digits: list[int], weights: tuple[int, ...]) -> int:
    remainder = sum(d * w for d, w in zip(digits, weights, strict=True)) % 11
    return 0 if remainder < 2 else 11 - remainder


def make_cnpj(seed: int | None = None) -> str:
    rng = random.Random(seed)  # noqa: S311  (test data, not a secret)
    base = [rng.randrange(10) for _ in range(12)]
    if len(set(base)) == 1:
        base[0] = (base[0] + 1) % 10
    first = _cnpj_check(base, CNPJ_FIRST_WEIGHTS)
    second = _cnpj_check([*base, first], CNPJ_SECOND_WEIGHTS)
    return "".join(map(str, [*base, first, second]))


def format_cpf(digits: str) -> str:
    return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"


def format_cnpj(digits: str) -> str:
    return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"


def with_bad_check_digit(digits: str) -> str:
    """The same number with its last digit changed, so the check digits no longer match."""
    return digits[:-1] + str((int(digits[-1]) + 1) % 10)
