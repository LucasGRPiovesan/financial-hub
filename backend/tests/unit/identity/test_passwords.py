from financial_hub.modules.identity.infrastructure.passwords import Argon2PasswordHasher

FAST = Argon2PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)


def test_default_parameters_are_the_owasp_argon2id_baseline() -> None:
    hasher = Argon2PasswordHasher()
    password_hash = hasher.hash("correct horse battery")
    assert password_hash.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert hasher.verify(password_hash, "correct horse battery")
    assert not hasher.needs_rehash(password_hash)


def test_hashes_are_salted() -> None:
    assert FAST.hash("same password!!") != FAST.hash("same password!!")


def test_wrong_passwords_and_garbage_hashes_verify_false() -> None:
    password_hash = FAST.hash("correct horse battery")
    assert not FAST.verify(password_hash, "wrong")
    assert not FAST.verify("not-a-hash", "anything")


def test_weaker_or_invalid_hashes_need_a_rehash() -> None:
    assert Argon2PasswordHasher().needs_rehash(FAST.hash("correct horse battery"))
    assert Argon2PasswordHasher().needs_rehash("not-a-hash")


def test_dummy_hash_is_a_stable_valid_hash_of_an_unknown_secret() -> None:
    dummy = FAST.dummy_hash
    assert dummy.startswith("$argon2id$")
    assert dummy == FAST.dummy_hash
    assert not FAST.verify(dummy, "")
