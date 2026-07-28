"""Test sikkerhetsventilen — den viktigste nye koden."""
from __future__ import annotations

from efaktura_radar.check import CheckResult, Status
from efaktura_radar.monitor import AnomalyGuard, run_once
from efaktura_radar.store import Store


def _ok(orgnr: str) -> CheckResult:
    return CheckResult(orgnr, "", Status.CAN_RECEIVE,
                       smp_url="https://smp.elma-smp.no/", on_elma=True)


def _gone(orgnr: str) -> CheckResult:
    return CheckResult(orgnr, "", Status.NOT_REGISTERED)


def _seed(n: int) -> tuple[Store, list[str]]:
    store = Store(":memory:", confirmations=1)
    orgnrs = [f"9{i:08d}" for i in range(n)]
    for o in orgnrs:
        store.watch("byra-a", o)
        store.record(_ok(o))
    # Datér tilbake, ellers regnes de som ferske og due() gir tom liste.
    store.conn.execute("UPDATE participant SET last_checked = '2020-01-01T00:00:00+00:00'")
    store.conn.commit()
    return store, orgnrs


def test_guard_thresholds() -> None:
    g = AnomalyGuard()
    assert not g.triggered(drops=3, eligible=10), "for lite utvalg"
    assert g.triggered(drops=5, eligible=30), "5 er absolutt minimum"
    assert not g.triggered(drops=4, eligible=30)
    assert g.triggered(drops=25, eligible=200), "10 % av 200"
    assert not g.triggered(drops=19, eligible=200)


def test_systemic_failure_is_rejected(monkeypatch) -> None:
    """SML-migrasjonen ryker -> alle NXDOMAIN. Ingenting skal skrives."""
    store, _ = _seed(50)
    import efaktura_radar.monitor as m
    monkeypatch.setattr(m, "check_many", lambda rows, **kw: (_gone(o) for o, _ in rows))

    before = len(store.changes_since("2000-01-01"))
    report = run_once(store, tenant="byra-a", stale_after_hours=24)

    assert report.anomaly is not None
    assert "systemsvikt" in report.anomaly
    assert report.changed == 0
    assert len(store.changes_since("2000-01-01")) == before, "ingenting skrevet"
    row = store.conn.execute("SELECT anomaly FROM run ORDER BY id DESC LIMIT 1").fetchone()
    assert row["anomaly"] == 1


def test_rejected_run_leaves_participants_due_for_retry(monkeypatch) -> None:
    store, _ = _seed(50)
    import efaktura_radar.monitor as m
    monkeypatch.setattr(m, "check_many", lambda rows, **kw: (_gone(o) for o, _ in rows))
    run_once(store, tenant="byra-a", stale_after_hours=24)
    assert len(store.due("byra-a", before="2099-01-01")) == 50


def test_a_few_real_deregistrations_pass_through(monkeypatch) -> None:
    """Ventilen skal ikke svelge ekte hendelser."""
    store, orgnrs = _seed(50)
    import efaktura_radar.monitor as m
    bad = set(orgnrs[:2])
    monkeypatch.setattr(
        m, "check_many",
        lambda rows, **kw: (_gone(o) if o in bad else _ok(o) for o, _ in rows))

    report = run_once(store, tenant="byra-a", stale_after_hours=24)
    assert report.anomaly is None
    assert report.changed == 2
    assert len(report.urgent) == 2


def test_mass_errors_are_not_treated_as_deregistration(monkeypatch) -> None:
    """Timeout er ikke avregistrering — ventilen skal ikke engang utløses."""
    store, _ = _seed(50)
    import efaktura_radar.monitor as m
    monkeypatch.setattr(
        m, "check_many",
        lambda rows, **kw: (CheckResult(o, "", Status.ERROR, error="Timeout") for o, _ in rows))

    report = run_once(store, tenant="byra-a", stale_after_hours=24)
    assert report.anomaly is None
    assert report.errors == 50
    assert report.changed == 0
