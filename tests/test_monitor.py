"""Kjøreløkka. Ingen nettverk — vi injiserer resultater direkte i lageret."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, tzinfo

import pytest

import efaktura_radar.monitor as monitor_mod
import efaktura_radar.store as store_mod
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


class _SameSecond(datetime):
    """Fryst klokke: begge rundene havner i samme sekund, hver gang."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> _SameSecond:
        return cls(2026, 10, 18, 4, 21, 7, 400_000, tzinfo=UTC)


def test_smp_round_right_after_dns_round_checks_everyone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regresjon: `kjor --full --intervall 0` rett etter DNS-runden i radar.yml.

    DNS-runden skriver last_checked med sekundoppløsning. Falt SMP-runden i
    samme sekund, ble cutoff lik last_checked, `last_checked < cutoff` usann for
    alle, og søndagens SMP-runde sjekket 0 motparter i 10 av 12 uker.
    """
    monkeypatch.setattr(monitor_mod, "datetime", _SameSecond)
    monkeypatch.setattr(store_mod, "datetime", _SameSecond)
    calls: list[tuple[list[str], bool]] = []

    def fake_check_many(
        rows: Iterable[tuple[str, str]], *, dns_only: bool, **kw: object
    ) -> Iterator[CheckResult]:
        orgnrs = [o for o, _ in rows]
        calls.append((orgnrs, dns_only))
        return (
            CheckResult(o, "", Status.CAN_RECEIVE, smp_url="https://smp.elma-smp.no/",
                        on_elma=True, verified=not dns_only)
            for o in orgnrs
        )

    monkeypatch.setattr(monitor_mod, "check_many", fake_check_many)
    orgnrs = ["923609016", "974760673", "986252932", "991825827"]

    with Store(":memory:") as store:
        for o in orgnrs:
            store.watch("byra-a", o)
        dns = monitor_mod.run_once(store, tenant="byra-a", stale_after_hours=20)
        smp = monitor_mod.run_once(
            store, tenant="byra-a", dns_only=False, stale_after_hours=0
        )

    assert dns.checked == len(orgnrs)
    assert smp.checked == len(orgnrs), "SMP-runden skal ta alle, også i samme sekund"
    assert [(sorted(seen), flag) for seen, flag in calls] == [(orgnrs, True), (orgnrs, False)]


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
