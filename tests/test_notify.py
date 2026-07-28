"""Varsling. Ingen nettverk, ingen SMTP — ConsoleSink samler opp i minnet.

De to viktigste testene her er at samme endring ikke varsles to ganger, og at
en mislykket sending ikke merker endringen som varslet. Begge handler om at
noen fortsatt skal lese mailen i måned tre.
"""

from __future__ import annotations

import contextlib

from efaktura_radar.check import CheckResult, Status
from efaktura_radar.notify import (
    ConsoleSink,
    Sink,
    SmtpSink,
    WebhookSink,
    notify,
    sink_from_env,
)
from efaktura_radar.store import ChangeKind, Store


def _res(
    orgnr: str = "986252932",
    status: str = Status.CAN_RECEIVE,
    *,
    smp: str | None = "https://smp.elma-smp.no/",
    elma: bool | None = True,
    name: str = "DFØ",
) -> CheckResult:
    return CheckResult(orgnr, name, status, smp_url=smp, on_elma=elma)


def _store_with_deregistration() -> tuple[Store, ConsoleSink]:
    store = Store(":memory:", confirmations=1)
    store.watch("byra-a", "986252932", label="DFØ")
    store.record(_res())
    store.record(_res(status=Status.NOT_REGISTERED, smp=None, elma=None))
    return store, ConsoleSink()


class FailingSink(Sink):
    """Sender aldri. Brukes til å bevise at vi ikke merker på forhånd."""

    def send(self, subject: str, body: str) -> None:
        raise RuntimeError("SMTP nede")


# ---------------------------------------------------------------- hastevarsel


def test_urgent_change_is_sent() -> None:
    store, sink = _store_with_deregistration()
    report = notify(store, tenant="byra-a", sink=sink, digest_weekday=None)

    assert report.urgent_sent == 1
    assert sink.sent is not None
    subject, body = sink.sent[0]
    assert "krever handling" in subject
    assert "kan ikke lenger motta e-faktura" in body
    assert "986252932" in body


def test_same_change_is_not_sent_twice() -> None:
    """Uten dette kommer samme varsel hver eneste natt til noen skrur det av."""
    store, sink = _store_with_deregistration()
    first = notify(store, tenant="byra-a", sink=sink, digest_weekday=None)
    second = notify(store, tenant="byra-a", sink=sink, digest_weekday=None)

    assert first.urgent_sent == 1
    assert second.urgent_sent == 0
    assert sink.sent is not None and len(sink.sent) == 1


def test_failed_send_leaves_change_unnotified() -> None:
    """Bedre å varsle sent enn å tape varselet stille."""
    store, _ = _store_with_deregistration()
    with contextlib.suppress(RuntimeError):
        notify(store, tenant="byra-a", sink=FailingSink(), digest_weekday=None)
    assert len(store.unnotified(tenant="byra-a", kinds=ChangeKind.URGENT)) == 1

    sink = ConsoleSink()
    report = notify(store, tenant="byra-a", sink=sink, digest_weekday=None)
    assert report.urgent_sent == 1


def test_non_urgent_changes_are_not_sent_as_urgent() -> None:
    store = Store(":memory:")
    store.watch("byra-a", "986252932")
    store.record(_res(status=Status.NOT_REGISTERED, smp=None, elma=None))
    store.record(_res())  # registrert — god nyhet, ikke hastesak
    sink = ConsoleSink()

    report = notify(store, tenant="byra-a", sink=sink, digest_weekday=None)
    assert report.urgent_sent == 0
    assert sink.sent is None or sink.sent == []


def test_urgent_scoped_to_tenant() -> None:
    store = Store(":memory:", confirmations=1)
    store.watch("byra-a", "986252932")
    store.record(_res("986252932"))
    store.record(_res("986252932", status=Status.NOT_REGISTERED, smp=None, elma=None))
    store.record(_res("991825827"))
    store.record(_res("991825827", status=Status.NOT_REGISTERED, smp=None, elma=None))

    sink = ConsoleSink()
    report = notify(store, tenant="byra-a", sink=sink, digest_weekday=None)
    assert report.urgent_sent == 1


# --------------------------------------------------------------------- digest


def test_digest_only_on_its_weekday() -> None:
    store = Store(":memory:")
    store.watch("byra-a", "986252932")
    store.record(_res())
    sink = ConsoleSink()

    from datetime import UTC, datetime

    other_day = (datetime.now(UTC).isoweekday() % 7) + 1
    report = notify(store, tenant="byra-a", sink=sink, digest_weekday=other_day)
    assert report.digest_sent == 0


def test_digest_sent_on_matching_weekday() -> None:
    from datetime import UTC, datetime

    store = Store(":memory:")
    store.watch("byra-a", "986252932", label="DFØ")
    store.record(_res())
    sink = ConsoleSink()

    today = datetime.now(UTC).isoweekday()
    report = notify(store, tenant="byra-a", sink=sink, digest_weekday=today)
    assert report.digest_sent == 1
    assert sink.sent is not None
    assert "Ukesoppdatering" in sink.sent[0][0]
    assert "Brønnøysundregistrene" in sink.sent[0][1], "NLOD krever attribusjon"


def test_empty_digest_is_not_sent() -> None:
    """52 «ingen endringer»-mailer i året får uke 53 filtrert bort."""
    from datetime import UTC, datetime

    store = Store(":memory:")
    store.watch("byra-a", "986252932")
    sink = ConsoleSink()

    report = notify(
        store, tenant="byra-a", sink=sink, digest_weekday=datetime.now(UTC).isoweekday()
    )
    assert report.digest_sent == 0
    assert sink.sent is None or sink.sent == []


# ------------------------------------------------------------------- avvik


def test_anomaly_sends_one_alert_and_no_change_notices() -> None:
    store, sink = _store_with_deregistration()
    report = notify(
        store,
        tenant="byra-a",
        sink=sink,
        digest_weekday=None,
        anomaly="Forkastet: 200 av 210 falt til «ikke registrert»",
    )

    assert report.urgent_sent == 0
    assert report.skipped_reason == "avvik"
    assert sink.sent is not None and len(sink.sent) == 1
    assert "forkastet" in sink.sent[0][0].lower()
    assert "31. august 2026" in sink.sent[0][1]
    # Endringene står fortsatt uvarslet — de skal ut når vi vet at de er ekte.
    assert len(store.unnotified(tenant="byra-a", kinds=ChangeKind.URGENT)) == 1


def test_stale_data_triggers_its_own_alert() -> None:
    store, sink = _store_with_deregistration()
    run_id = store.start_run("dns")
    store.finish_run(run_id, checked=1, changed=0, errors=0)
    store.conn.execute("UPDATE run SET finished_at = '2020-01-01T00:00:00+00:00'")
    store.conn.commit()

    notify(store, tenant="byra-a", sink=sink, digest_weekday=None)
    assert sink.sent is not None
    assert "Ingen ferske data" in sink.sent[0][0]
    assert "60 dager" in sink.sent[0][1]


# --------------------------------------------------------------- kanalvalg


def test_sink_from_env_defaults_to_console() -> None:
    assert isinstance(sink_from_env({}), ConsoleSink)


def test_sink_from_env_picks_webhook() -> None:
    sink = sink_from_env({"RADAR_WEBHOOK_URL": "https://hooks.example/x"})
    assert isinstance(sink, WebhookSink)


def test_sink_from_env_picks_smtp() -> None:
    sink = sink_from_env(
        {
            "RADAR_SMTP_HOST": "smtp.example",
            "RADAR_SMTP_TO": "a@b.no, c@d.no",
            "RADAR_SMTP_USER": "u",
            "RADAR_SMTP_PASS": "p",
        }
    )
    assert isinstance(sink, SmtpSink)
    assert sink.recipients == ["a@b.no", "c@d.no"]
    assert sink.sender == "u", "FROM faller tilbake til brukernavnet"
    assert sink.port == 587


def test_smtp_without_recipients_falls_back_to_console() -> None:
    """Halvkonfigurert SMTP skal ikke gi et varsel som forsvinner i intet."""
    sink = sink_from_env({"RADAR_SMTP_HOST": "smtp.example"})
    assert isinstance(sink, ConsoleSink)


def test_webhook_wins_over_smtp() -> None:
    sink = sink_from_env(
        {"RADAR_WEBHOOK_URL": "https://hooks.example/x", "RADAR_SMTP_HOST": "smtp"}
    )
    assert isinstance(sink, WebhookSink)


def test_anomaly_is_read_from_store_without_being_passed_in() -> None:
    """«varsle» skal ikke trenge at «kjor» treder teksten gjennom shellet."""
    store, sink = _store_with_deregistration()
    run_id = store.start_run("dns")
    store.flag_anomaly(run_id, "Forkastet: 40 av 42 falt til «ikke registrert»")
    store.finish_run(run_id, checked=42, changed=0, errors=0)

    report = notify(store, tenant="byra-a", sink=sink, digest_weekday=None)
    assert report.skipped_reason == "avvik"
    assert sink.sent is not None and "forkastet" in sink.sent[0][0].lower()


def test_healthy_run_after_anomaly_resumes_notifications() -> None:
    store, sink = _store_with_deregistration()
    bad = store.start_run("dns")
    store.flag_anomaly(bad, "Forkastet")
    store.finish_run(bad, checked=42, changed=0, errors=0)
    good = store.start_run("dns")
    store.finish_run(good, checked=42, changed=0, errors=0)

    report = notify(store, tenant="byra-a", sink=sink, digest_weekday=None)
    assert report.urgent_sent == 1
