"""Peppol SML-oppslag: organisasjonsnummer -> SMP-adresse, via DNS.

Dette er det autoritative første steget. Ingen API-nøkkel, ingen registrering,
ingen publiserte ratebegrensninger — bare DNS.

## To migrasjoner som begge lander i 2026

OpenPeppol har byttet BÅDE hash-algoritme OG DNS-sone, og de to endringene
blandes ofte sammen i leverandørdokumentasjon:

=============  ==============================  ===================================
               Gammel                          Ny
=============  ==============================  ===================================
Hash + record  MD5, CNAME                      SHA-256 -> Base32, U-NAPTR
Sone (prod)    ``edelivery.tech.ec.europa.eu`` ``participant.sml.prod.tech.peppol.org``
=============  ==============================  ===================================

**Den gamle EC-sonen slutter å virke 31. august 2026.** Kode som fortsatt
slår opp der er allerede utdatert. Vi implementerer NAPTR først, med CNAME
som fallback fram til cutover.

Hash-inputen er *kun verdidelen* av deltakeridentifikatoren — ``0192:986252932``
— ikke hele ``iso6523-actorid-upis::0192:986252932``. Dette er den vanligste
implementasjonsfeilen.

Begge algoritmene er verifisert mot den offisielle testvektoren i
«Peppol CNAME to NAPTR Migration Process v1.0.0» (se tests/test_sml.py).
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import date

import dns.exception
import dns.resolver

from .orgnr import normalise

__all__ = [
    "EC_SUNSET",
    "LEGACY_ZONE_PROD",
    "SCHEME",
    "ZONE_PROD",
    "ZONE_TEST",
    "SmlResult",
    "legacy_hostname",
    "naptr_hostname",
    "participant_id",
    "resolve_smp",
]

SCHEME = "iso6523-actorid-upis"
ICD_NORWAY = "0192"

ZONE_PROD = "participant.sml.prod.tech.peppol.org"
ZONE_TEST = "participant.sml.test.tech.peppol.org"
LEGACY_ZONE_PROD = "edelivery.tech.ec.europa.eu"

#: Datoen den gamle EC-sonen slutter å svare. Fjern fallback-koden etter dette.
EC_SUNSET = date(2026, 8, 31)

#: U-NAPTR service-feltet. Spesifikasjonen tillater ingen andre verdier.
NAPTR_SERVICE = "Meta:SMP"

#: ELMA publiserer `!.*!<url>!` (verifisert live 2026-07-28), men RFC 4848
#: tillater også ankerformen `!^.*$!<url>!`. Godta begge — en SMP som bruker
#: RFC-formen ville ellers gitt smp_url=None og dermed falsk «forlot_elma».
_NAPTR_REPLACEMENT = re.compile(r'!\^?\.\*\$?!(?P<url>[^!]+)!', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SmlResult:
    """Utfallet av et SML-oppslag for én virksomhet."""

    orgnr: str
    registered: bool
    smp_url: str | None
    hostname: str
    #: `None` ved suksess eller NXDOMAIN; ellers unntaksnavnet (timeout o.l.).
    error: str | None = None

    @property
    def on_elma(self) -> bool:
        """Ligger virksomheten på ELMA, eller på en annen SMP?

        Dette skillet er kommersielt viktig. Forarbeidene til bokføringsloven
        (Prop. 44 L, kap. 3.4) navngir **ELMA** spesifikt som utløseren for
        sendeplikten, mens Digdir selv presiserer at norske virksomheter godt
        kan ligge på en annen SMP og være fullt mottakelige. En virksomhet på
        en kommersiell SMP er nåbar via Peppol, men faller utenfor
        forarbeidenes ordlyd. Det er et uavklart punkt — og et av de mest
        interessante funnene i hele analysen.
        """
        return bool(self.smp_url and "elma-smp.no" in self.smp_url)


def participant_id(orgnr: str) -> str:
    """Bygg full Peppol-deltakeridentifikator: ``iso6523-actorid-upis::0192:NNNNNNNNN``."""
    return f"{SCHEME}::{ICD_NORWAY}:{normalise(orgnr)}"


def _identifier_value(orgnr: str) -> str:
    """Verdidelen alene — dette, og bare dette, er hash-inputen."""
    return f"{ICD_NORWAY}:{normalise(orgnr)}"


def naptr_hostname(orgnr: str, zone: str = ZONE_PROD) -> str:
    """Gjeldende (SHA-256/Base32) DNS-navn, per PFUOI v4.4.0 POLICY 7."""
    digest = hashlib.sha256(_identifier_value(orgnr).lower().encode("utf-8")).digest()
    label = base64.b32encode(digest).decode("ascii").rstrip("=")
    return f"{label}.{SCHEME}.{zone}"


def legacy_hostname(orgnr: str, zone: str = LEGACY_ZONE_PROD) -> str:
    """Utgått (MD5/CNAME) DNS-navn. Dør 31. august 2026 — kun fallback."""
    digest = hashlib.md5(_identifier_value(orgnr).lower().encode("utf-8")).hexdigest()
    return f"B-{digest}.{SCHEME}.{zone}"


def resolve_smp(
    orgnr: str,
    *,
    zone: str = ZONE_PROD,
    timeout: float = 10.0,
    resolver: dns.resolver.Resolver | None = None,
    legacy_fallback: bool = False,
) -> SmlResult:
    """Slå opp hvilken SMP en virksomhet er registrert på.

    NXDOMAIN betyr at virksomheten ikke er registrert hos noen Peppol-SMP —
    altså at den **ikke kan motta e-faktura**. Det er det svaret produktet
    selger.

    Sett ``legacy_fallback=True`` for også å prøve den gamle EC-sonen. Det er
    bare relevant fram til 31. august 2026, og bør deretter fjernes.
    """
    res = resolver or dns.resolver.Resolver()
    host = naptr_hostname(orgnr, zone)

    try:
        answer = res.resolve(host, "NAPTR", lifetime=timeout)
    except dns.resolver.NXDOMAIN:
        if legacy_fallback and zone == ZONE_PROD:
            return _resolve_legacy(orgnr, res, timeout, host)
        return SmlResult(normalise(orgnr), False, None, host)
    except dns.exception.DNSException as exc:
        return SmlResult(normalise(orgnr), False, None, host, error=type(exc).__name__)

    return SmlResult(normalise(orgnr), True, _extract_url(answer), host)


def _resolve_legacy(
    orgnr: str,
    res: dns.resolver.Resolver,
    timeout: float,
    naptr_host: str,
) -> SmlResult:
    host = legacy_hostname(orgnr)
    try:
        res.resolve(host, "CNAME", lifetime=timeout)
    except dns.resolver.NXDOMAIN:
        return SmlResult(normalise(orgnr), False, None, naptr_host)
    except dns.exception.DNSException as exc:
        return SmlResult(normalise(orgnr), False, None, host, error=type(exc).__name__)
    # CNAME-formen peker på SMP-verten uten å oppgi URL-en direkte.
    return SmlResult(normalise(orgnr), True, None, host)


def _extract_url(answer: dns.resolver.Answer) -> str | None:
    """Hent SMP-URL-en ut av U-NAPTR-regexpen ``!.*!https://host/!``."""
    for record in answer:
        text = record.to_text()
        if NAPTR_SERVICE.lower() not in text.lower():
            continue
        match = _NAPTR_REPLACEMENT.search(text)
        if match:
            return match.group("url").rstrip("/") + "/"
    return None
