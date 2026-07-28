"""Kjøreløkka. Ingen nettverk — vi injiserer resultater direkte i lageret."""

from __future__ import annotations

from efaktura_radar.check import CheckResult, Status
from efaktura_radar.monitor import format_digest
from efaktura_radar.store import ChangeKind, Store


def _r(orgnr: str, status: str = Status.CAN_RECEIVE, name: str = "") -> CheckResult:
    return CheckResult(
        orgnr, name, status,
        smp_url="https://smp.elma-smp.no/" if status != Status.NOT_REGISTERED else None,
        on_elma=True if status != Status.NOT_REGISTERED else None,
    )


def test_run_with_empty_watchlist_is_harmless() -> None:
    from efaktura_radar.monitor import run_once

    with Store(":memory:") as store:
        report = run_once(store, tenant="byra-a")
    assert report.checked == 0 and report.changes == []


def test_digest_is_empty_when_nothing_changed() -> None:
    assert format_digest([]) == "Ingen endringer i perioden."


def test_digest_puts_urgent_first() -> None:
    with Store(":memory:", confirmations=1) as store:
        store.watch("byra-a", "986252932")
        store.watch("byra-a", "991825827")
        store.record(_r("986252932", name="DFØ"))
        store.record(_r("991825827", name="Digdir"))
        store.record(_r("986252932", Status.NOT_REGISTERED, name="DFØ"))
        changes = store.changes_since("2000-01-01", tenant="byra-a")

    text = format_digest(changes, tenant="byra-a")
    assert "KREVER HANDLING" in text
    assert text.index("KREVER HANDLING") < text.index("Øvrige endringer")
    assert ChangeKind.DEREGISTERED in text
    assert "Brønnøysundregistrene" in text, "NLOD 2.0 krever attribusjon"


def test_digest_scoped_to_tenant() -> None:
    with Store(":memory:") as store:
        store.watch("byra-a", "986252932")
        store.record(_r("986252932"))
        store.record(_r("991825827"))
        assert len(store.changes_since("2000-01-01", tenant="byra-a")) == 1
        assert len(store.changes_since("2000-01-01")) == 2
