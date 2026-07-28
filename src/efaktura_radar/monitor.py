"""Kjøreløkka: overvåkningsliste -> oppslag -> sikkerhetsventil -> endringslogg.

Dette er det cron-jobben kaller.

Spre lasten. ``batch_size`` og ``stale_after_hours`` gjør at 5 000 motparter
kan gå over en uke i stedet for i én støt hver natt. Digdirs eneste publiserte
råd er «oppslag ved behov — ikkje køyre store batch-jobbar», og i Norge ligger
nesten alle på samme SMP-vert, så belastningen konsentreres uansett.

## Sikkerhetsventilen — den viktigste koden i fila

Bekreftelsesregelen i `store.py` beskytter mot at *én* motpart flapper. Den
beskytter ikke mot **systemsvikt**, for da er den andre observasjonen like feil
som den første.

Det er ikke et hypotetisk problem. Den gamle SML-sonen slutter å svare
**31. august 2026**. Går noe galt i den migrasjonen, returnerer hvert eneste
NAPTR-oppslag NXDOMAIN — og etter to kjøringer ville systemet «bekreftet» at
samtlige kunder har mistet EHF-tilgangen og sendt varsel om det.

Derfor: hvis en uforholdsmessig andel av dem som *hadde* status faller til
«ikke registrert» i én kjøring, forkastes hele kjøringen. Ingenting skrives.
Kjøringen merkes som avvik, og motpartene står fortsatt som utdaterte, så neste
kjøring prøver dem på nytt.

Å tape en dags observasjoner er billig. Å sende 200 falske «kunden din kan ikke
lenger motta faktura» er ikke.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .check import CheckResult, Status, check_many
from .store import Change, Store, utc_now

__all__ = ["AnomalyGuard", "RunReport", "digest", "format_digest", "run_once"]


@dataclass(frozen=True, slots=True)
class AnomalyGuard:
    """Terskler for når en kjøring forkastes som systemsvikt.

    Standardverdiene er satt ut fra at avregistrering fra Peppol er sjeldent.
    En virksomhet som først har registrert seg pleier å bli. Mange samtidige
    fall er derfor langt mer sannsynlig et infrastrukturproblem hos oss enn en
    reell hendelse hos kundene.
    """

    #: Andel av de som hadde status som må falle før vi mistenker systemsvikt.
    ratio: float = 0.10
    #: Aldri utløs på færre enn dette, uansett andel.
    minimum: int = 5
    #: Krev et minimum av datapunkter før andelen betyr noe.
    min_sample: int = 20

    def triggered(self, drops: int, eligible: int) -> bool:
        if eligible < self.min_sample:
            return False
        return drops >= max(self.minimum, round(self.ratio * eligible))


@dataclass(frozen=True, slots=True)
class RunReport:
    run_id: int
    checked: int
    changed: int
    errors: int
    changes: list[Change] = field(default_factory=list)
    #: Satt når sikkerhetsventilen forkastet kjøringen.
    anomaly: str | None = None

    @property
    def urgent(self) -> list[Change]:
        """Endringene som fortjener en e-post i dag, ikke i månedsrapporten."""
        return [c for c in self.changes if c.is_urgent]


def run_once(
    store: Store,
    *,
    tenant: str | None = None,
    dns_only: bool = True,
    stale_after_hours: int = 24,
    batch_size: int = 1_000,
    workers: int = 8,
    guard: AnomalyGuard | None = None,
    note: str | None = None,
) -> RunReport:
    """Sjekk de motpartene som står for tur, og logg endringene.

    ``dns_only=True`` er standard med vilje: DNS svarer allerede på det
    kommersielt viktige (registrert? hvilken SMP? ELMA eller ikke?), det er
    raskt, og det belaster ingen tredjeparts SMP. Kjør en full runde med
    ``dns_only=False`` sjeldnere — månedlig holder — for å bekrefte at det
    faktisk er *faktura* de kan motta.
    """
    guard = guard or AnomalyGuard()
    cutoff = (
        (datetime.now(UTC) - timedelta(hours=stale_after_hours))
        .replace(microsecond=0)
        .isoformat()
    )
    due = store.due(tenant, before=cutoff, limit=batch_size)

    mode = "dns" if dns_only else "dns+smp"
    run_id = store.start_run(mode, note)

    if not due:
        store.finish_run(run_id, checked=0, changed=0, errors=0)
        return RunReport(run_id, 0, 0, 0)

    labels = _labels(store, due)
    results: list[CheckResult] = list(
        check_many(
            ((orgnr, labels.get(orgnr, "")) for orgnr in due),
            dns_only=dns_only,
            workers=workers,
        )
    )
    errors = sum(
        1 for r in results if r.status in (Status.ERROR, Status.INVALID_ORGNR)
    )

    # Sikkerhetsventilen kjører FØR noe skrives.
    verdict = _check_for_anomaly(store, results, guard)
    if verdict is not None:
        store.flag_anomaly(run_id, verdict)
        store.finish_run(run_id, checked=len(results), changed=0, errors=errors)
        return RunReport(run_id, len(results), 0, errors, anomaly=verdict)

    changes: list[Change] = []
    for result in results:
        changes.extend(store.record(result, run_id=run_id))

    store.finish_run(
        run_id, checked=len(results), changed=len(changes), errors=errors
    )
    return RunReport(run_id, len(results), len(changes), errors, changes)


def _check_for_anomaly(
    store: Store, results: list[CheckResult], guard: AnomalyGuard
) -> str | None:
    """Ser dette ut som at kundene endret seg, eller som at vi er i stykker?"""
    known = store.current_statuses(r.orgnr for r in results)
    eligible = drops = 0
    for result in results:
        previous = known.get(result.orgnr)
        if previous not in (Status.CAN_RECEIVE, Status.REGISTERED_NO_INVOICE):
            continue
        eligible += 1
        if result.status == Status.NOT_REGISTERED:
            drops += 1

    if not guard.triggered(drops, eligible):
        return None
    return (
        f"Forkastet: {drops} av {eligible} med kjent status falt til "
        f"«ikke registrert» i én kjøring. Mistenker systemsvikt (DNS/SML), "
        f"ikke reelle avregistreringer. Ingenting ble skrevet."
    )


def _labels(store: Store, orgnrs: list[str]) -> dict[str, str]:
    """Navn fra overvåkningslista, med Enhetsregisteret som reserve."""
    marks = ",".join("?" * len(orgnrs))
    rows = store.conn.execute(
        f"""SELECT w.orgnr, COALESCE(NULLIF(w.label, ''), p.brreg_name, '') AS name
            FROM watch w LEFT JOIN participant p ON p.orgnr = w.orgnr
            WHERE w.removed_at IS NULL AND w.orgnr IN ({marks})""",
        orgnrs,
    )
    return {r["orgnr"]: r["name"] for r in rows}


def digest(store: Store, tenant: str, *, days: int = 7) -> list[Change]:
    """Endringene siden sist — innholdet i ukesmailen til byrået."""
    since = (
        (datetime.now(UTC) - timedelta(days=days)).replace(microsecond=0).isoformat()
    )
    return store.changes_since(since, tenant=tenant)


def format_digest(changes: list[Change], *, tenant: str = "") -> str:
    """Ren tekst, klar for e-post. Haster først."""
    if not changes:
        return "Ingen endringer i perioden."

    urgent = [c for c in changes if c.is_urgent]
    rest = [c for c in changes if not c.is_urgent]
    lines: list[str] = []

    if tenant:
        lines.append(f"E-fakturastatus — {tenant}")
        lines.append("")
    if urgent:
        lines.append(f"KREVER HANDLING ({len(urgent)})")
        lines.extend(_line(c) for c in urgent)
        lines.append("")
    if rest:
        lines.append(f"Øvrige endringer ({len(rest)})")
        lines.extend(_line(c) for c in rest)
        lines.append("")
    lines.append(f"Generert {utc_now()}.")
    lines.append(
        "Kilde: Peppol SML/SMP. Navn fra Enhetsregisteret — inneholder data under "
        "norsk lisens for offentlige data (NLOD) tilgjengeliggjort av "
        "Brønnøysundregistrene."
    )
    return "\n".join(lines)


def _line(change: Change) -> str:
    who = f"{change.orgnr} {change.name}".strip()
    date = change.observed_at[:10]
    return f"  {date}  {change.kind:24s} {who}"
