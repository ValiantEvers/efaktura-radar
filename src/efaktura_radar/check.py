"""Full oppslagskjede: organisasjonsnummer -> kan motta EHF-faktura?

    orgnr -> validering -> SML (DNS/NAPTR) -> SMP (HTTPS) -> dokumenttyper

Steg 1 alene (DNS) svarer allerede på det viktigste: er virksomheten
registrert i Peppol i det hele tatt, og hos hvilken SMP. Det er raskt, gratis
og uten avtaleforhold. Steg 2 bekrefter at det faktisk er *faktura* de kan
motta — de aller fleste som er registrert støtter faktura, men ikke alle.

Sett ``dns_only=True`` for et hurtigsveip over en stor kundeliste, og kjør
steg 2 kun på de som treffer.
"""

from __future__ import annotations

import concurrent.futures as futures
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass

import httpx

from .orgnr import InvalidOrgnr, normalise
from .sml import SmlResult, resolve_smp
from .smp import fetch_service_group

__all__ = ["CheckResult", "Status", "check", "check_many"]


class Status:
    """Utfallskategorier. Strenger, ikke enum — de skal rett i CSV."""

    CAN_RECEIVE = "kan_motta"
    REGISTERED_NO_INVOICE = "registrert_uten_faktura"
    NOT_REGISTERED = "ikke_registrert"
    INVALID_ORGNR = "ugyldig_orgnr"
    ERROR = "feil"


@dataclass(slots=True)
class CheckResult:
    orgnr: str
    name: str
    status: str
    smp_url: str | None = None
    on_elma: bool | None = None
    doctype_count: int = 0
    can_receive_credit_note: bool = False
    other_capabilities: str = ""
    error: str | None = None

    def as_row(self) -> dict[str, object]:
        return asdict(self)


def check(
    orgnr: str,
    name: str = "",
    *,
    dns_only: bool = False,
    client: httpx.Client | None = None,
    timeout: float = 10.0,
) -> CheckResult:
    """Sjekk én virksomhet."""
    try:
        clean = normalise(orgnr)
    except InvalidOrgnr as exc:
        return CheckResult(orgnr, name, Status.INVALID_ORGNR, error=str(exc))

    sml: SmlResult = resolve_smp(clean, timeout=timeout)
    if sml.error:
        return CheckResult(clean, name, Status.ERROR, error=sml.error)
    if not sml.registered:
        return CheckResult(clean, name, Status.NOT_REGISTERED)

    base = CheckResult(
        orgnr=clean,
        name=name,
        status=Status.CAN_RECEIVE,
        smp_url=sml.smp_url,
        on_elma=sml.on_elma,
    )

    # DNS-only: registrert i Peppol er i praksis synonymt med å kunne motta
    # faktura, men vi merker ikke resultatet som verifisert.
    if dns_only or not sml.smp_url:
        return base

    try:
        group = fetch_service_group(sml.smp_url, clean, client=client, timeout=timeout)
    except httpx.HTTPError as exc:
        base.error = type(exc).__name__
        return base

    if group is None:
        # DNS sier registrert, SMP sier 404. Reell inkonsistens — verdt å logge.
        base.status = Status.NOT_REGISTERED
        base.error = "SMP 404 tross NAPTR-treff"
        return base

    base.doctype_count = len(group.doctypes)
    base.can_receive_credit_note = group.can_receive_credit_note
    base.other_capabilities = ",".join(group.other_capabilities)
    base.status = (
        Status.CAN_RECEIVE if group.can_receive_invoice else Status.REGISTERED_NO_INVOICE
    )
    return base


def check_many(
    rows: Iterable[tuple[str, str]],
    *,
    dns_only: bool = False,
    workers: int = 8,
    timeout: float = 10.0,
) -> Iterator[CheckResult]:
    """Sjekk mange virksomheter parallelt.

    Hold ``workers`` moderat. DNS tåler mye, men steg 2 treffer tredjeparts
    SMP-er du ikke har avtale med — og i Norge ligger nesten alle på samme
    vert, så belastningen konsentreres.
    """
    with (
        httpx.Client(timeout=timeout, follow_redirects=True) as client,
        futures.ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        jobs = [
            pool.submit(check, orgnr, name, dns_only=dns_only, client=client, timeout=timeout)
            for orgnr, name in rows
        ]
        for job in futures.as_completed(jobs):
            yield job.result()
