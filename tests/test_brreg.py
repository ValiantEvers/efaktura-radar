"""Brreg-berikelse. Ingen nettverk — httpx.MockTransport svarer i stedet."""

from __future__ import annotations

import json

import httpx
import pytest

from efaktura_radar.brreg import ATTRIBUTION, enrich_store, fetch_batch
from efaktura_radar.check import CheckResult, Status
from efaktura_radar.store import Store

ENHET = {
    "organisasjonsnummer": "923609016",
    "navn": "EQUINOR ASA",
    "organisasjonsform": {"kode": "ASA"},
    "naeringskode1": {"kode": "06.100"},
    "antallAnsatte": 21327,
    "konkurs": False,
    "underAvvikling": False,
}
UNDERENHET = {
    "organisasjonsnummer": "974760673",
    "navn": "AVDELING X",
    "organisasjonsform": {"kode": "BEDR"},
    "konkurs": False,
}


def _transport(
    enheter: list[dict[str, object]], underenheter: list[dict[str, object]] | None = None
) -> httpx.MockTransport:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        wanted = set(request.url.params.get("organisasjonsnummer", "").split(","))
        key = "underenheter" if request.url.path.endswith("/underenheter") else "enheter"
        pool = underenheter or [] if key == "underenheter" else enheter
        hits = [e for e in pool if e["organisasjonsnummer"] in wanted]
        body: dict[str, object] = {"page": {"totalElements": len(hits)}}
        if hits:
            body["_embedded"] = {key: hits}
        return httpx.Response(200, content=json.dumps(body))

    transport = httpx.MockTransport(handler)
    transport.calls = calls  # type: ignore[attr-defined]
    return transport


def _client(transport: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=transport)


def _seen(orgnr: str) -> CheckResult:
    return CheckResult(
        orgnr, "", Status.CAN_RECEIVE, smp_url="https://smp.elma-smp.no/", on_elma=True
    )


def test_fetch_batch_parses_embedded() -> None:
    with _client(_transport([ENHET])) as client:
        hits = fetch_batch(["923609016"], client=client)
    assert hits["923609016"]["navn"] == "EQUINOR ASA"


def test_empty_response_has_no_embedded_key() -> None:
    """Brreg utelater `_embedded` helt når ingenting matcher."""
    with _client(_transport([])) as client:
        assert fetch_batch(["986252932"], client=client) == {}


def test_fetch_batch_with_no_input_makes_no_call() -> None:
    transport = _transport([ENHET])
    with _client(transport) as client:
        assert fetch_batch([], client=client) == {}
    assert transport.calls == []  # type: ignore[attr-defined]


def test_enrich_store_fills_fields() -> None:
    with Store(":memory:") as store:
        store.watch("byra-a", "923609016")
        store.record(_seen("923609016"))
        with _client(_transport([ENHET])) as client:
            report = enrich_store(store, tenant="byra-a", client=client)

        assert report.found_enhet == 1
        row = store.conn.execute(
            "SELECT brreg_name, brreg_ansatte, brreg_kind FROM participant WHERE orgnr = ?",
            ("923609016",),
        ).fetchone()
        assert row["brreg_name"] == "EQUINOR ASA"
        assert row["brreg_ansatte"] == 21327
        assert row["brreg_kind"] == "enhet"


def test_falls_back_to_underenheter() -> None:
    """En kundeliste kan inneholde avdelinger — de ligger i et eget register."""
    with Store(":memory:") as store:
        store.watch("byra-a", "974760673")
        with _client(_transport([], [UNDERENHET])) as client:
            report = enrich_store(store, tenant="byra-a", client=client)

        assert report.found_underenhet == 1
        assert report.found_enhet == 0
        row = store.conn.execute(
            "SELECT brreg_name, brreg_kind FROM participant WHERE orgnr = ?",
            ("974760673",),
        ).fetchone()
        assert row["brreg_kind"] == "underenhet"


def test_unknown_orgnr_is_timestamped_not_retried() -> None:
    """Ukjente skal ikke slås opp på nytt hver eneste måned."""
    with Store(":memory:") as store:
        store.watch("byra-a", "986252932")
        with _client(_transport([])) as client:
            first = enrich_store(store, tenant="byra-a", client=client)
        assert first.unknown == 1

        transport = _transport([])
        with _client(transport) as client:
            second = enrich_store(store, tenant="byra-a", client=client)
        assert second.requested == 0
        assert transport.calls == []  # type: ignore[attr-defined]


def test_enrichment_does_not_touch_peppol_status() -> None:
    """Berikelse er pynt. Den skal aldri kunne endre det som overvåkes."""
    with Store(":memory:") as store:
        store.watch("byra-a", "923609016")
        store.record(_seen("923609016"))
        before = store.changes_since("2000-01-01")
        with _client(_transport([ENHET])) as client:
            enrich_store(store, tenant="byra-a", client=client)

        after = store.changes_since("2000-01-01")
        assert len(after) == len(before)
        row = store.conn.execute(
            "SELECT status FROM participant WHERE orgnr = ?", ("923609016",)
        ).fetchone()
        assert row["status"] == Status.CAN_RECEIVE


def test_http_error_is_counted_not_raised() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with Store(":memory:") as store:
        store.watch("byra-a", "923609016")
        with httpx.Client(transport=httpx.MockTransport(boom)) as client:
            report = enrich_store(store, tenant="byra-a", client=client)
        assert report.errors == 1 and report.found == 0


def test_flagged_lists_bankrupt_customers() -> None:
    bankrupt = {**ENHET, "organisasjonsnummer": "986252932", "navn": "KONK AS", "konkurs": True}
    with Store(":memory:") as store:
        store.watch("byra-a", "986252932")
        with _client(_transport([bankrupt])) as client:
            enrich_store(store, tenant="byra-a", client=client)
        assert store.flagged("byra-a") == [("986252932", "KONK AS", "konkurs")]


def test_batching_splits_large_lists() -> None:
    """5 000 kunder skal bli ~50 kall, ikke 5 000."""
    many = [
        {**ENHET, "organisasjonsnummer": f"{n}"} for n in range(900000000, 900000250)
    ]
    transport = _transport(many)
    with Store(":memory:") as store:
        for e in many:
            store.watch("byra-a", str(e["organisasjonsnummer"]))
        # Hopp over mod11 — vi tester batching, ikke validering.
        store.conn.execute("UPDATE watch SET removed_at = NULL")
        with _client(transport) as client:
            enrich_store(store, tenant="byra-a", client=client)
    assert 3 <= len(transport.calls) <= 6, transport.calls  # type: ignore[attr-defined]


def test_attribution_string_names_the_source() -> None:
    assert "Brønnøysundregistrene" in ATTRIBUTION
    assert "NLOD" in ATTRIBUTION


@pytest.mark.live
def test_live_brreg_batch() -> None:
    hits = fetch_batch(["923609016", "986252932", "991825827"])
    assert len(hits) == 3
    assert hits["923609016"]["navn"] == "EQUINOR ASA"
