"""SMP-oppslag: hvilke dokumenttyper kan denne virksomheten faktisk motta?

Steg to. Ett HTTPS GET mot ServiceGroup-ressursen lister alle dokumenttyper
mottakeren støtter, som `href`-URL-er. Vi trenger normalt ikke å hente hver
enkelt ServiceMetadata.

Én ekte fallgruve: en registrering kan eksistere uten å være aktiv. Endepunkter
har valgfrie `ServiceActivationDate` / `ServiceExpirationDate`, og de finnes
bare i den detaljerte ServiceMetadata-ressursen — ikke i ServiceGroup. Vil du
være helt sikker på at mottakeren er aktiv i dag, må du gjøre steg (b).
For et regnskapsbyrå-dashboard er ServiceGroup godt nok, og ti ganger billigere.

Kodingsregelen fra SMP-spesifikasjonen § 5.3.1: hver seksjon mellom skråstreker
prosentkodes hver for seg. Skråstrekene skal *ikke* kodes.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime
from xml.etree import ElementTree

import httpx

from . import doctypes as dt
from .sml import participant_id

__all__ = [
    "NS",
    "ServiceGroup",
    "endpoint_is_active",
    "fetch_service_group",
    "parse_service_group",
    "service_group_url",
    "service_metadata_url",
]

NS = {
    "smp": "http://busdox.org/serviceMetadata/publishing/1.0/",
    "ids": "http://busdox.org/transport/identifiers/1.0/",
    "wsa": "http://www.w3.org/2005/08/addressing",
}

_USER_AGENT = "efaktura-radar/0.1 (+https://evers.no)"


@dataclass(slots=True)
class ServiceGroup:
    """Alle dokumenttyper en mottaker har registrert."""

    orgnr: str
    doctypes: list[str] = field(default_factory=list)

    @property
    def can_receive_invoice(self) -> bool:
        return dt.supports_ehf_invoice(self.doctypes)

    @property
    def can_receive_credit_note(self) -> bool:
        return dt.CREDIT_NOTE in self.doctypes

    @property
    def other_capabilities(self) -> list[str]:
        """Navn på øvrige post-award-prosesser mottakeren støtter."""
        known = {v: k for k, v in dt.OTHER_POST_AWARD.items()}
        return sorted({known[d] for d in self.doctypes if d in known})


def service_group_url(smp_base: str, orgnr: str) -> str:
    """`GET <smp>/{prosentkodet deltaker-id}` — lister alle dokumenttyper."""
    pid = urllib.parse.quote(participant_id(orgnr), safe="")
    return f"{smp_base.rstrip('/')}/{pid}"


def service_metadata_url(smp_base: str, orgnr: str, doctype: str) -> str:
    """`GET <smp>/{deltaker}/services/{doctype}` — én type, med gyldighetsdatoer."""
    pid = urllib.parse.quote(participant_id(orgnr), safe="")
    did = urllib.parse.quote(f"{dt.DOCTYPE_SCHEME}::{doctype}", safe="")
    return f"{smp_base.rstrip('/')}/{pid}/services/{did}"


def fetch_service_group(
    smp_base: str,
    orgnr: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 15.0,
) -> ServiceGroup | None:
    """Hent og tolk ServiceGroup. `None` ved 404 (ingen registrering).

    SMP-ene driftes av kommersielle tredjeparter, og du har ingen avtale med
    dem. Digdirs eneste publiserte råd er uformelt: «oppslag ved behov — ikkje
    køyre store batch-jobbar». Cache aggressivt og respekter DNS-TTL.
    """
    url = service_group_url(smp_base, orgnr)
    owns_client = client is None
    client = client or httpx.Client(
        timeout=timeout, headers={"User-Agent": _USER_AGENT}, follow_redirects=True
    )
    try:
        response = client.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return parse_service_group(response.text, orgnr)
    finally:
        if owns_client:
            client.close()


def parse_service_group(xml: str, orgnr: str) -> ServiceGroup:
    """Hent dokumenttypene ut av ServiceMetadataReference-URL-ene.

    Hver `href` slutter på `/services/{prosentkodet busdox-docid-qns::<type>}`.
    Vi dekoder halen og stripper skjemaprefikset.
    """
    root = ElementTree.fromstring(xml)
    found: list[str] = []
    prefix = f"{dt.DOCTYPE_SCHEME}::"

    for ref in root.iter(f"{{{NS['smp']}}}ServiceMetadataReference"):
        href = ref.get("href")
        if not href or "/services/" not in href:
            continue
        decoded = urllib.parse.unquote(href.rsplit("/services/", 1)[1])
        found.append(decoded.removeprefix(prefix))

    return ServiceGroup(orgnr=orgnr, doctypes=found)


def endpoint_is_active(xml: str, on: date | None = None) -> bool:
    """Er endepunktet i en ServiceMetadata-respons gyldig på gitt dato?

    Manglende `ServiceActivationDate` betyr «gyldig fra alltid», manglende
    `ServiceExpirationDate` betyr «gyldig til evig tid». Dette er den eneste
    reelle kilden til falske positiver i steg (a).
    """
    on = on or date.today()
    root = ElementTree.fromstring(xml)
    for endpoint in root.iter(f"{{{NS['smp']}}}Endpoint"):
        start = _parse_date(endpoint.findtext(f"{{{NS['smp']}}}ServiceActivationDate"))
        end = _parse_date(endpoint.findtext(f"{{{NS['smp']}}}ServiceExpirationDate"))
        if (start is None or start <= on) and (end is None or end >= on):
            return True
    return False


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).date()
    except ValueError:
        return None
