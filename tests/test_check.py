"""Full kjede-logikk. Ingen nettverk — resolve_smp byttes ut, SMP-en mockes.

Det viktigste her er regel 3-grensen: en SMP-timeout under en full runde må
bli `feil`, ikke et optimistisk «kan_motta». Ellers kan en timeout både
nullstille en ventende regresjon og utløse falsk «fikk_fakturastotte».
"""

from __future__ import annotations

import importlib
from pathlib import Path

import httpx
import pytest

from efaktura_radar.check import Status, check
from efaktura_radar.sml import SmlResult

# Pakkens __init__ re-eksporterer funksjonen `check`, som skygger for
# submodulen med samme navn — hent modulobjektet eksplisitt for monkeypatch.
check_mod = importlib.import_module("efaktura_radar.check")

FIXTURE = (
    Path(__file__).parent / "fixtures" / "servicegroup_elma.xml"
).read_text(encoding="utf-8")


def _registered(orgnr: str = "986252932") -> SmlResult:
    return SmlResult(orgnr, True, "https://smp.elma-smp.no/", "host.example")


def test_smp_http_error_is_an_error_not_an_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regel 3: en SMP-timeout er ikke en observasjon av at alt er i orden."""
    monkeypatch.setattr(check_mod, "resolve_smp", lambda o, **kw: _registered())

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("SMP svarer ikke", request=request)

    with httpx.Client(transport=httpx.MockTransport(boom)) as client:
        result = check("986252932", client=client)

    assert result.status == Status.ERROR
    assert result.error == "ConnectTimeout"
    assert not result.verified


def test_dns_only_result_is_not_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registrert i DNS er en antakelse om faktura — lagringslaget må vite det."""
    monkeypatch.setattr(check_mod, "resolve_smp", lambda o, **kw: _registered())
    result = check("986252932", dns_only=True)
    assert result.status == Status.CAN_RECEIVE
    assert not result.verified


def test_nxdomain_is_verified_not_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """NXDOMAIN er autoritativt: ingen SMP, ingen mottak. Fullverdig observasjon."""
    monkeypatch.setattr(
        check_mod,
        "resolve_smp",
        lambda o, **kw: SmlResult("986252932", False, None, "host.example"),
    )
    result = check("986252932")
    assert result.status == Status.NOT_REGISTERED
    assert result.verified


def test_smp_response_marks_result_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_mod, "resolve_smp", lambda o, **kw: _registered())

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=FIXTURE.encode())

    with httpx.Client(transport=httpx.MockTransport(ok)) as client:
        result = check("986252932", client=client)

    assert result.status == Status.CAN_RECEIVE
    assert result.verified
    assert result.doctype_count == 3


def test_smp_404_is_a_verified_inconsistency(monkeypatch: pytest.MonkeyPatch) -> None:
    """DNS sier registrert, SMP sier 404 — reell inkonsistens, ikke en feil."""
    monkeypatch.setattr(check_mod, "resolve_smp", lambda o, **kw: _registered())

    def gone(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(gone)) as client:
        result = check("986252932", client=client)

    assert result.status == Status.NOT_REGISTERED
    assert result.verified
    assert result.error == "SMP 404 tross NAPTR-treff"
