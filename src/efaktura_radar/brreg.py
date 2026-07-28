"""Berikelse fra Enhetsregisteret.

Orgnr fra en kundelisteeksport er ofte alt byrået har. Navn, organisasjonsform,
næringskode og konkursstatus gjør rapporten lesbar — og sammenstillingen
orgnr → navn → EHF-kapabilitet er nettopp den ingen selger i dag.

## Hvorfor batch

Søkeendepunktet tar en kommaseparert liste:

    GET /enhetsregisteret/api/enheter?organisasjonsnummer=A,B,C&size=100

Det gjør 5 000 kunder til ~50 kall i stedet for 5 000. Verifisert mot API-et
2026-07-27.

## Fallgruver

**Underenheter.** En kundeliste kan inneholde underenheter (avdelinger), som
ikke ligger i ``/enheter``. De må hentes fra ``/underenheter``. Vi prøver
hovedregisteret først og faller tilbake.

**Manglende treff er ikke en feil.** Et orgnr som ikke finnes noe sted får
``kind='ukjent'`` og et tidsstempel, slik at vi ikke slår det opp på nytt hver
måned. Byråets eget navn fra kundelista beholdes.

**Paginering.** ``(page+1)*size`` kan ikke overskride 10 000. Vi holder oss
langt under ved å batche på orgnr i stedet for å bla.

## Lisens

NLOD 2.0. Kommersiell bruk er eksplisitt tillatt, men attribusjon kreves ved
videreformidling — se ``ATTRIBUTION``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .store import Store

__all__ = ["ATTRIBUTION", "BASE_URL", "EnrichReport", "enrich_store", "fetch_batch"]

BASE_URL = "https://data.brreg.no/enhetsregisteret/api"

#: Påkrevd ved videreformidling, NLOD 2.0 § 5.
ATTRIBUTION = (
    "Inneholder data under norsk lisens for offentlige data (NLOD) "
    "tilgjengeliggjort av Brønnøysundregistrene."
)

_USER_AGENT = "efaktura-radar/0.1 (+https://evers.no)"

#: Brreg tar 9-sifrede orgnr kommaseparert. 100 holder god margin på URL-lengden.
_BATCH = 100


@dataclass(slots=True)
class EnrichReport:
    requested: int = 0
    found_enhet: int = 0
    found_underenhet: int = 0
    unknown: int = 0
    errors: int = 0

    @property
    def found(self) -> int:
        return self.found_enhet + self.found_underenhet


def fetch_batch(
    orgnrs: Sequence[str],
    *,
    client: httpx.Client | None = None,
    endpoint: str = "enheter",
    timeout: float = 30.0,
) -> dict[str, dict[str, Any]]:
    """Hent inntil ``_BATCH`` virksomheter i ett kall. Ukjente uteblir stille."""
    if not orgnrs:
        return {}
    owns = client is None
    client = client or httpx.Client(
        timeout=timeout, headers={"User-Agent": _USER_AGENT}, follow_redirects=True
    )
    try:
        response = client.get(
            f"{BASE_URL}/{endpoint}",
            params={"organisasjonsnummer": ",".join(orgnrs), "size": len(orgnrs)},
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns:
            client.close()

    # Tom respons har ingen `_embedded`-nøkkel i det hele tatt.
    items = (payload.get("_embedded") or {}).get(endpoint) or []
    return {
        str(item["organisasjonsnummer"]): item
        for item in items
        if item.get("organisasjonsnummer")
    }


def enrich_store(
    store: Store,
    *,
    tenant: str | None = None,
    max_age_days: int = 30,
    limit: int = 5_000,
    client: httpx.Client | None = None,
    timeout: float = 30.0,
) -> EnrichReport:
    """Berik de som mangler eller har utdaterte registerdata.

    Kjør sjeldnere enn statussjekken. Enhetsregisteret endrer seg langsomt, og
    navn er pynt — det skal aldri stjele budsjett fra det som faktisk overvåkes.
    """
    before = (datetime.now(UTC) - timedelta(days=max_age_days)).replace(
        microsecond=0
    ).isoformat()
    todo = store.needs_enrichment(tenant, before=before, limit=limit)
    report = EnrichReport(requested=len(todo))
    if not todo:
        return report

    owns = client is None
    client = client or httpx.Client(
        timeout=timeout, headers={"User-Agent": _USER_AGENT}, follow_redirects=True
    )
    try:
        for chunk in _chunks(todo, _BATCH):
            try:
                hits = fetch_batch(chunk, client=client, endpoint="enheter")
            except httpx.HTTPError:
                report.errors += len(chunk)
                continue

            for orgnr, data in hits.items():
                store.enrich(orgnr, data, kind="enhet")
            report.found_enhet += len(hits)

            missing = [o for o in chunk if o not in hits]
            if not missing:
                continue

            # Kan være avdelinger — de ligger i et eget register.
            try:
                sub = fetch_batch(missing, client=client, endpoint="underenheter")
            except httpx.HTTPError:
                report.errors += len(missing)
                continue

            for orgnr, data in sub.items():
                store.enrich(orgnr, data, kind="underenhet")
            report.found_underenhet += len(sub)

            for orgnr in missing:
                if orgnr not in sub:
                    store.enrich(orgnr, {}, kind="ukjent")
                    report.unknown += 1
    finally:
        if owns:
            client.close()
    return report


def _chunks(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
