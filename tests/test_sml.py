"""Testvektorer for SML-navngenerering.

Vektorene under er hentet ordrett fra OpenPeppols egen migrasjonsspesifikasjon,
«Peppol CNAME to NAPTR Migration Process v1.0.0» (2025-04-17), for deltakeren
``iso6523-actorid-upis::0088:123abc``. Hvis disse ryker, er algoritmen feil —
ikke testen.
"""

from __future__ import annotations

import pytest

from efaktura_radar.sml import (
    LEGACY_ZONE_PROD,
    SCHEME,
    ZONE_PROD,
    legacy_hostname,
    naptr_hostname,
    participant_id,
    resolve_smp,
)

# Offisiell testvektor. Skjemadelen er 0088 (GLN), ikke 0192 — vi tester
# algoritmen, ikke Norge-spesifikk logikk, så vi kaller de private hjelperne
# via en liten omvei.
SPEC_VALUE = "0088:123abc"
SPEC_MD5 = "B-f5e78500450d37de5aabe6648ac3bb70"
SPEC_B32 = "Y7DZFXAF3D4CJZ4KCGRXTEC6TWVCGA4KY7ZWA5BOIF6MSWD4TDRQ"


def _b32_label(value: str) -> str:
    import base64
    import hashlib

    return base64.b32encode(hashlib.sha256(value.lower().encode()).digest()).decode().rstrip("=")


def _md5_label(value: str) -> str:
    import hashlib

    return "B-" + hashlib.md5(value.lower().encode()).hexdigest()


def test_sha256_base32_matches_spec_vector() -> None:
    assert _b32_label(SPEC_VALUE) == SPEC_B32


def test_md5_matches_spec_vector() -> None:
    assert _md5_label(SPEC_VALUE) == SPEC_MD5


def test_base32_label_is_52_chars_without_padding() -> None:
    label = naptr_hostname("986252932").split(".", 1)[0]
    assert len(label) == 52
    assert "=" not in label


def test_hash_input_is_value_only_not_full_identifier() -> None:
    """Den vanligste implementasjonsfeilen: å hashe hele deltaker-ID-en."""
    correct = naptr_hostname("986252932").split(".", 1)[0]
    wrong = _b32_label(f"{SCHEME}::0192:986252932")
    assert correct != wrong
    assert correct == _b32_label("0192:986252932")


def test_participant_id_format() -> None:
    assert participant_id("986 252 932") == "iso6523-actorid-upis::0192:986252932"


def test_naptr_hostname_structure() -> None:
    host = naptr_hostname("986252932")
    assert host.endswith(f".{SCHEME}.{ZONE_PROD}")


def test_legacy_hostname_structure() -> None:
    host = legacy_hostname("986252932")
    assert host.startswith("B-")
    assert host.endswith(f".{SCHEME}.{LEGACY_ZONE_PROD}")


def test_known_norwegian_hostnames_are_stable() -> None:
    """Regresjonslås. Verifisert mot live DNS 2026-07-27."""
    assert naptr_hostname("986252932").startswith(
        "JGPA45E5TP5QNMA3C3XUXV6V4ZE4LHDYW6AL2HZYU3NRLUFX3D3A."
    )
    assert naptr_hostname("991825827").startswith(
        "7I2243UOLQ5QZB6JXFVI6YEI4IQ7SFIPKV55HTZFNR6S54Y6YUFA."
    )


@pytest.mark.live
def test_live_lookup_dfo() -> None:
    """DFØ er registrert på ELMA. Krever nettverk."""
    result = resolve_smp("986252932")
    assert result.registered
    assert result.on_elma


def test_naptr_regexp_accepts_both_wildcard_forms() -> None:
    """ELMA publiserer `!.*!…!` (verifisert live 2026-07-28), men RFC 4848
    tillater også ankerformen `!^.*$!…!`. Begge må gi SMP-URL — ellers står en
    fullt gyldig registrering som «utenfor ELMA», med falskt hastevarsel."""
    from efaktura_radar.sml import _NAPTR_REPLACEMENT

    live = '100 10 "U" "Meta:SMP" "!.*!https://smp.elma-smp.no/!" .'
    rfc = '100 10 "U" "Meta:SMP" "!^.*$!https://smp.example.org/!" .'
    m_live = _NAPTR_REPLACEMENT.search(live)
    m_rfc = _NAPTR_REPLACEMENT.search(rfc)
    assert m_live and m_live.group("url") == "https://smp.elma-smp.no/"
    assert m_rfc and m_rfc.group("url") == "https://smp.example.org/"
