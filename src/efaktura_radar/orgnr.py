"""Validering og normalisering av norske organisasjonsnummer.

Peppol krever formatet `[0-9]{9}` (ICD 0192, «NO:ORG», utstedt av
Brønnøysundregistrene). Kontrollsifferet er mod11 med vektene 3,2,7,6,5,4,3,2.

Å validere lokalt er gratis og sparer et DNS-oppslag per ugyldig rad — nyttig
når inputen er en kundeliste eksportert fra et regnskapssystem.
"""

from __future__ import annotations

import re

__all__ = ["MOD11_WEIGHTS", "InvalidOrgnr", "is_valid", "normalise"]

MOD11_WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)

_NON_DIGIT = re.compile(r"[^0-9]")


class InvalidOrgnr(ValueError):
    """Reist når en streng ikke er et gyldig norsk organisasjonsnummer."""


def normalise(raw: str) -> str:
    """Fjern mellomrom og skilletegn, og returner ni siffer.

    Regnskapssystemer eksporterer orgnr på mange former: «986 252 932»,
    «NO986252932MVA», «986252932». Alle skal ende samme sted.

    Reiser :class:`InvalidOrgnr` hvis resultatet ikke er ni gyldige siffer.
    """
    digits = _NON_DIGIT.sub("", raw or "")
    # «NO986252932MVA» -> 986252932 etter at bokstavene er strippet.
    if len(digits) != 9:
        raise InvalidOrgnr(f"forventet 9 siffer, fikk {len(digits)}: {raw!r}")
    if not _check_digit_ok(digits):
        raise InvalidOrgnr(f"kontrollsiffer stemmer ikke: {raw!r}")
    return digits


def is_valid(raw: str) -> bool:
    """Som :func:`normalise`, men returnerer bool i stedet for å reise unntak."""
    try:
        normalise(raw)
    except InvalidOrgnr:
        return False
    return True


def _check_digit_ok(digits: str) -> bool:
    total = sum(int(d) * w for d, w in zip(digits[:8], MOD11_WEIGHTS, strict=True))
    remainder = total % 11
    control = 0 if remainder == 0 else 11 - remainder
    if control == 10:
        # Kontrollsiffer 10 kan ikke representeres — nummeret utstedes aldri.
        return False
    return control == int(digits[8])
