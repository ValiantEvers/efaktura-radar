"""Tester for endringsloggen.

De viktigste testene her er ikke de som sjekker at endringer *blir* logget,
men de som sjekker at de **ikke** blir det: et DNS-timeout skal aldri kunne bli
til «kunden mistet EHF-tilgang». Falske alarmer er den raskeste måten å drepe
et overvåkingsprodukt på.
"""

from __future__ import annotations

import pytest

from efaktura_radar.check import CheckResult, Status
from efaktura_radar.store import ChangeKind, Store, utc_now


def result(
    orgnr: str = "986252932",
    status: str = Status.CAN_RECEIVE,
    *,
    smp: str | None = "https://smp.elma-smp.no/",
    elma: bool | None = True,
    name: str = "DFØ",
) -> CheckResult:
    return CheckResult(orgnr, name, status, smp_url=smp, on_elma=elma)


@pytest.fixture
def store() -> Store:
    return Store(":memory:", confirmations=2)


# ------------------------------------------------------------------ grunnlag


def test_first_observation_is_recorded_as_new(store: Store) -> None:
    changes = store.record(result())
    assert len(changes) == 1
    assert changes[0].kind == ChangeKind.NEW_CAN_RECEIVE


def test_unchanged_observation_records_nothing(store: Store) -> None:
    store.record(result())
    assert store.record(result()) == []
    assert store.record(result()) == []


def test_new_participant_not_registered(store: Store) -> None:
    changes = store.record(result(status=Status.NOT_REGISTERED, smp=None, elma=None))
    assert changes[0].kind == ChangeKind.NEW_CANNOT_RECEIVE


# --------------------------------------------------- regel 2: bekreftelse


def test_deregistration_requires_confirmation(store: Store) -> None:
    """Én observasjon er ikke nok til å melde at noen har mistet tilgangen."""
    store.record(result())
    first = store.record(result(status=Status.NOT_REGISTERED, smp=None, elma=None))
    assert first == [], "regresjon skal ikke logges på første observasjon"

    second = store.record(result(status=Status.NOT_REGISTERED, smp=None, elma=None))
    assert len(second) == 1
    assert second[0].kind == ChangeKind.DEREGISTERED


def test_flapping_does_not_produce_a_change(store: Store) -> None:
    """Nede én gang, oppe igjen neste gang: ingen endring skal logges."""
    store.record(result())
    store.record(result(status=Status.NOT_REGISTERED, smp=None, elma=None))
    store.record(result())  # tilbake
    assert store.changes_since("2000-01-01") == [
        c for c in store.changes_since("2000-01-01") if c.kind == ChangeKind.NEW_CAN_RECEIVE
    ]


def test_confirmation_counter_resets_on_recovery(store: Store) -> None:
    store.record(result())
    store.record(result(status=Status.NOT_REGISTERED, smp=None, elma=None))
    store.record(result())
    # Ny nedtur må igjen bekreftes fra scratch.
    assert store.record(result(status=Status.NOT_REGISTERED, smp=None, elma=None)) == []


def test_improvement_is_recorded_immediately(store: Store) -> None:
    """Forbedringer er ufarlige — de skal ikke vente på bekreftelse."""
    store.record(result(status=Status.NOT_REGISTERED, smp=None, elma=None))
    changes = store.record(result())
    assert len(changes) == 1
    assert changes[0].kind == ChangeKind.REGISTERED


def test_confirmations_one_disables_the_buffer(store: Store) -> None:
    immediate = Store(":memory:", confirmations=1)
    immediate.record(result())
    changes = immediate.record(result(status=Status.NOT_REGISTERED, smp=None, elma=None))
    assert changes[0].kind == ChangeKind.DEREGISTERED


# ------------------------------------------------------- regel 3: feil


def test_error_never_writes_a_change(store: Store) -> None:
    store.record(result())
    err = CheckResult("986252932", "DFØ", Status.ERROR, error="Timeout")
    assert store.record(err) == []
    assert store.record(err) == []
    assert store.record(err) == []
    row = store.conn.execute(
        "SELECT status, consecutive_errors FROM participant WHERE orgnr = ?",
        ("986252932",),
    ).fetchone()
    assert row["status"] == Status.CAN_RECEIVE, "feil skal ikke røre statusen"
    assert row["consecutive_errors"] == 3


def test_error_on_unknown_participant_does_not_invent_status(store: Store) -> None:
    store.record(CheckResult("986252932", "", Status.ERROR, error="Timeout"))
    row = store.conn.execute(
        "SELECT status FROM participant WHERE orgnr = ?", ("986252932",)
    ).fetchone()
    assert row["status"] is None


def test_invalid_orgnr_is_not_a_status(store: Store) -> None:
    bad = CheckResult("123456789", "", Status.INVALID_ORGNR, error="kontrollsiffer")
    assert store.record(bad) == []


# ------------------------------------------------------------ SMP og ELMA


def test_smp_change_is_detected(store: Store) -> None:
    store.record(result())
    changes = store.record(result(smp="https://sml.ion-smp.net/", elma=False))
    kinds = {c.kind for c in changes}
    assert ChangeKind.CHANGED_SMP in kinds
    assert ChangeKind.LEFT_ELMA in kinds


def test_leaving_elma_is_urgent(store: Store) -> None:
    """Forarbeidene navngir ELMA spesifikt — dette må byrået få vite om."""
    store.record(result())
    changes = store.record(result(smp="https://sml.ion-smp.net/", elma=False))
    assert any(c.is_urgent for c in changes if c.kind == ChangeKind.LEFT_ELMA)


def test_joining_elma_is_not_urgent(store: Store) -> None:
    store.record(result(smp="https://sml.ion-smp.net/", elma=False))
    changes = store.record(result())
    joined = [c for c in changes if c.kind == ChangeKind.JOINED_ELMA]
    assert joined and not joined[0].is_urgent


def test_off_elma_listing(store: Store) -> None:
    store.record(result("986252932"))
    store.record(result("991825827", smp="https://sml.ion-smp.net/", elma=False))
    assert store.off_elma() == ["991825827"]


# ------------------------------------------------------------ historikk


def test_status_at_answers_historical_questions(store: Store) -> None:
    """Hele poenget med SCD2 — og grunnen til å begynne å samle nå."""
    store.record(result(status=Status.NOT_REGISTERED, smp=None, elma=None))
    early = utc_now()
    store.conn.execute("UPDATE change SET observed_at = '2026-01-01T00:00:00+00:00'")
    store.conn.commit()

    store.record(result())
    store.record(result())
    store.conn.execute(
        "UPDATE change SET observed_at = '2026-06-01T00:00:00+00:00'"
        " WHERE new_value = ? AND observed_at > '2026-01-02'",
        (Status.CAN_RECEIVE,),
    )
    store.conn.commit()

    assert store.status_at("986252932", "2026-03-01T00:00:00+00:00") == Status.NOT_REGISTERED
    assert store.status_at("986252932", "2026-09-01T00:00:00+00:00") == Status.CAN_RECEIVE
    assert store.status_at("986252932", "2025-01-01T00:00:00+00:00") is None
    assert early


# ------------------------------------------------------ overvåkningsliste


def test_watchlist_scopes_changes_by_tenant(store: Store) -> None:
    store.watch("byra-a", "986252932")
    store.watch("byra-b", "991825827")
    store.record(result("986252932"))
    store.record(result("991825827"))

    a = store.changes_since("2000-01-01", tenant="byra-a")
    assert [c.orgnr for c in a] == ["986252932"]


def test_unwatch_is_soft(store: Store) -> None:
    store.watch("byra-a", "986252932")
    store.record(result("986252932"))
    store.unwatch("byra-a", "986252932")
    assert store.watched("byra-a") == []
    # Historikken består.
    assert store.changes_since("2000-01-01") != []


def test_rewatch_reactivates(store: Store) -> None:
    store.watch("byra-a", "986252932")
    store.unwatch("byra-a", "986252932")
    store.watch("byra-a", "986252932")
    assert store.watched("byra-a") == ["986252932"]


def test_import_watchlist(store: Store) -> None:
    rows = [("986252932", "DFØ"), ("991825827", "Digdir")]
    assert store.import_watchlist("byra-a", rows, client_ref="klient-1") == 2
    assert len(store.watched("byra-a")) == 2


# --------------------------------------------------- regel 1: dekning


def test_coverage_distinguishes_unchecked_from_unchanged(store: Store) -> None:
    store.watch("byra-a", "986252932")
    store.watch("byra-a", "991825827")
    store.record(result("986252932"))

    cov = store.coverage("byra-a")
    assert cov.watched == 2
    assert cov.checked_ever == 1
    assert cov.never_checked == 1


def test_coverage_flags_stale_data(store: Store) -> None:
    store.watch("byra-a", "986252932")
    store.record(result("986252932"))
    store.conn.execute("UPDATE participant SET last_ok = '2020-01-01T00:00:00+00:00'")
    store.conn.commit()
    cov = store.coverage("byra-a", stale_before="2026-01-01T00:00:00+00:00")
    assert cov.stale == 1


def test_due_prioritises_never_checked(store: Store) -> None:
    store.watch("byra-a", "986252932")
    store.watch("byra-a", "991825827")
    store.record(result("986252932"))
    due = store.due("byra-a", before="2099-01-01T00:00:00+00:00")
    assert due[0] == "991825827"


def test_due_respects_batch_size(store: Store) -> None:
    for orgnr in ("986252932", "991825827", "974760673"):
        store.watch("byra-a", orgnr)
    assert len(store.due("byra-a", before="2099-01-01", limit=2)) == 2


def test_due_skips_freshly_checked(store: Store) -> None:
    store.watch("byra-a", "986252932")
    store.record(result("986252932"))
    assert store.due("byra-a", before="2000-01-01T00:00:00+00:00") == []


# ----------------------------------------------------------- rapportering


def test_urgent_filter(store: Store) -> None:
    store.record(result("986252932"))
    store.record(result("986252932", status=Status.NOT_REGISTERED, smp=None, elma=None))
    store.record(result("986252932", status=Status.NOT_REGISTERED, smp=None, elma=None))
    urgent = store.changes_since("2000-01-01", urgent_only=True)
    assert [c.kind for c in urgent] == [ChangeKind.DEREGISTERED]


def test_summary_counts_by_status(store: Store) -> None:
    store.record(result("986252932"))
    store.record(result("991825827", status=Status.NOT_REGISTERED, smp=None, elma=None))
    assert store.summary() == {Status.CAN_RECEIVE: 1, Status.NOT_REGISTERED: 1}


def test_enrich_stores_brreg_fields(store: Store) -> None:
    store.record(result("923609016"))
    store.enrich(
        "923609016",
        {
            "navn": "EQUINOR ASA",
            "organisasjonsform": {"kode": "ASA"},
            "naeringskode1": {"kode": "06.100"},
            "antallAnsatte": 21327,
            "konkurs": False,
        },
    )
    row = store.conn.execute(
        "SELECT brreg_name, brreg_ansatte, brreg_checked FROM participant WHERE orgnr = ?",
        ("923609016",),
    ).fetchone()
    assert row["brreg_name"] == "EQUINOR ASA"
    assert row["brreg_ansatte"] == 21327
    assert row["brreg_checked"]


def test_run_lifecycle(store: Store) -> None:
    run_id = store.start_run("dns", note="test")
    store.record(result(), run_id=run_id)
    store.finish_run(run_id, checked=1, changed=1, errors=0)
    row = store.conn.execute("SELECT * FROM run WHERE id = ?", (run_id,)).fetchone()
    assert row["finished_at"] and row["n_checked"] == 1
    assert store.coverage().last_run_at == row["finished_at"]


def test_digest_falls_back_to_watchlist_label_for_name(store: Store) -> None:
    """Før Brreg-berikelsen har kjørt er byråets egen etikett alt vi har."""
    store.watch("byra-a", "986252932", label="DFØ")
    store.record(result("986252932", name=""))
    change = store.changes_since("2000-01-01", tenant="byra-a")[0]
    assert change.name == "DFØ"


def test_brreg_name_wins_over_watchlist_label(store: Store) -> None:
    store.watch("byra-a", "923609016", label="equinor (skrivefeil)")
    store.record(result("923609016", name=""))
    store.enrich("923609016", {"navn": "EQUINOR ASA"})
    change = store.changes_since("2000-01-01", tenant="byra-a")[0]
    assert change.name == "EQUINOR ASA"


def test_store_creates_missing_parent_directory(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Første cron-kjøring har ingen data/-mappe. Uten dette dør den der."""
    target = tmp_path / "data" / "radar.db"
    assert not target.parent.exists()
    with Store(target) as store:
        store.record(result())
    assert target.exists()
