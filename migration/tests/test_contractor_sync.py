import pytest

from migration.contractor_sync import (
    _build_update,
    _preserve_blank,
    normalize_email,
    normalize_phone,
    parse_bool,
)


def test_normalize_email():
    assert normalize_email("  USER@Example.COM ") == "user@example.com"
    assert normalize_email("bad-email") is None
    assert normalize_email("") is None


def test_normalize_phone():
    assert normalize_phone("(312) 555-1212") == "3125551212"
    assert normalize_phone("+1 312 555 1212") == "3125551212"
    assert normalize_phone(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Yes", True),
        ("TRUE", True),
        ("1", True),
        ("No", False),
        ("false", False),
        ("0", False),
        ("", None),
        ("Maybe", None),
        (None, None),
    ],
)
def test_parse_bool(raw, expected):
    assert parse_bool(raw) is expected


def test_blank_preservation():
    assert _preserve_blank("existing", "") == "existing"
    assert _preserve_blank("existing", None) == "existing"
    assert _preserve_blank("existing", "new") == "new"


def test_build_update_preserves_blanks_and_unknown_bools():
    existing = {
        "name": "Jane Doe",
        "phone": "3125551212",
        "location": "Chicago, IL",
        "ic_list": "A",
        "pod_color": "Blue",
        "digital_certified": True,
        "unrestricted": False,
    }
    source = {
        "name": "Jane Doe",
        "phone": "",
        "location": None,
        "ic_list": "B",
        "pod_color": "Green",
        "digital_certified": None,
        "unrestricted": True,
    }
    assert _build_update(existing, source) == {
        "ic_list": "B",
        "pod_color": "Green",
        "unrestricted": True,
    }
