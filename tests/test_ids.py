import pytest

from eridi.core.ids import is_ulid, new_id, new_ulid, split_id


def test_ulids_sort_in_creation_order_and_are_unique() -> None:
    ids = [new_ulid() for _ in range(10_000)]
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)
    assert all(is_ulid(i) for i in ids)


def test_prefixed_ids_round_trip() -> None:
    for prefix in ("root", "proj", "dev"):
        value = new_id(prefix)  # type: ignore[arg-type]
        got_prefix, ulid = split_id(value)
        assert got_prefix == prefix
        assert is_ulid(ulid)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "proj",
        "proj_",
        "proj_123",
        "xyz_01JC8Z9K3V5W6X7Y8Z9A0B1C2D",
        "proj_01jc8z9k3v5w6x7y8z9a0b1c2d",
    ],
)
def test_split_id_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError, match="not a valid ID"):
        split_id(bad)


def test_unknown_prefix_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown ID prefix"):
        new_id("file")  # type: ignore[arg-type]


def test_is_ulid_rejects_lowercase_and_ambiguous_letters() -> None:
    good = new_ulid()
    assert is_ulid(good)
    assert not is_ulid(good.lower())
    assert not is_ulid("I" * 26)  # I, L, O, U are not Crockford base32
