"""mod11-kontroll av norske organisasjonsnummer."""

from __future__ import annotations

import pytest

from efaktura_radar.orgnr import InvalidOrgnr, is_valid, normalise

# Ekte, verifiserbare organisasjonsnummer.
VALID = ["986252932", "991825827", "974760673", "923609016", "978655424"]


@pytest.mark.parametrize("orgnr", VALID)
def test_real_org_numbers_validate(orgnr: str) -> None:
    assert is_valid(orgnr)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("986 252 932", "986252932"),
        ("986252932", "986252932"),
        ("NO986252932MVA", "986252932"),
        ("986.252.932", "986252932"),
        ("  986252932\n", "986252932"),
    ],
)
def test_normalise_strips_formatting(raw: str, expected: str) -> None:
    assert normalise(raw) == expected


@pytest.mark.parametrize(
    "bad",
    ["123456789", "986252933", "", "12345678", "1234567890", "abcdefghi"],
)
def test_invalid_is_rejected(bad: str) -> None:
    assert not is_valid(bad)
    with pytest.raises(InvalidOrgnr):
        normalise(bad)


def test_control_digit_ten_is_never_valid() -> None:
    """Kontrollsiffer 10 kan ikke representeres; slike nummer utstedes ikke."""
    from efaktura_radar.orgnr import MOD11_WEIGHTS

    for candidate in range(10_000_000, 10_000_400):
        digits = f"{candidate:08d}"
        total = sum(int(d) * w for d, w in zip(digits, MOD11_WEIGHTS, strict=True))
        remainder = total % 11
        if remainder == 1:  # gir kontrollsiffer 10
            for last in range(10):
                assert not is_valid(digits + str(last))
            return
    pytest.skip("fant ingen kandidat i intervallet")
