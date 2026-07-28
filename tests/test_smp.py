"""Tolkning av SMP-svar, mot en fiksturfil som speiler ELMAs faktiske format."""

from __future__ import annotations

from pathlib import Path

from efaktura_radar import doctypes as dt
from efaktura_radar.smp import (
    endpoint_is_active,
    parse_service_group,
    service_group_url,
    service_metadata_url,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "servicegroup_elma.xml").read_text(encoding="utf-8")


def test_parses_all_doctypes() -> None:
    group = parse_service_group(FIXTURE, "986252932")
    assert len(group.doctypes) == 3


def test_detects_invoice_capability() -> None:
    group = parse_service_group(FIXTURE, "986252932")
    assert group.can_receive_invoice
    assert group.can_receive_credit_note
    assert dt.INVOICE in group.doctypes


def test_lists_other_post_award_processes() -> None:
    group = parse_service_group(FIXTURE, "986252932")
    assert group.other_capabilities == ["order"]


def test_credit_note_alone_is_not_invoice_capability() -> None:
    only_credit = FIXTURE.replace("AInvoice-2%3A%3AInvoice", "ACreditNote-2%3A%3ACreditNote")
    group = parse_service_group(only_credit, "986252932")
    assert not group.can_receive_invoice


def test_service_group_url_encodes_each_section() -> None:
    url = service_group_url("https://smp.elma-smp.no/", "986252932")
    assert url == (
        "https://smp.elma-smp.no/iso6523-actorid-upis%3A%3A0192%3A986252932"
    )


def test_service_metadata_url_encodes_hash_and_colons() -> None:
    url = service_metadata_url("https://smp.elma-smp.no", "986252932", dt.INVOICE)
    assert "/services/" in url
    assert "%23%23" in url  # ## fra dokumenttypen
    assert url.count("/services/") == 1


def test_endpoint_without_dates_is_active_forever() -> None:
    xml = """<smp:SignedServiceMetadata xmlns:smp="http://busdox.org/serviceMetadata/publishing/1.0/">
      <smp:Endpoint transportProfile="peppol-transport-as4-v2_0"/>
    </smp:SignedServiceMetadata>"""
    assert endpoint_is_active(xml)


def test_expired_endpoint_is_not_active() -> None:
    xml = """<smp:SignedServiceMetadata xmlns:smp="http://busdox.org/serviceMetadata/publishing/1.0/">
      <smp:Endpoint transportProfile="peppol-transport-as4-v2_0">
        <smp:ServiceExpirationDate>2020-01-01T00:00:00Z</smp:ServiceExpirationDate>
      </smp:Endpoint>
    </smp:SignedServiceMetadata>"""
    assert not endpoint_is_active(xml)
