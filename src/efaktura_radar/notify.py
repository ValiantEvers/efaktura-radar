"""Varsling: få de viktige endringene ut til et menneske.

## To kanaler, med vilje

**Hastevarsel** går ut samme dag: `avregistrert`, `mistet_fakturastotte`,
`forlot_elma`. Det er de tre som brekker sendeplikt-flyten fra 1. januar 2027.

**Ukesdigest** tar resten. Ingen vil ha e-post fordi en kunde *fikk* EHF-støtte.

## Regelen som avgjør om noen fortsatt leser mailen i måned tre

Én endring gir ett varsel. `change.notified_at` settes **etter** vellykket
sending, aldri før. Feiler sendingen, står endringen igjen som uvarslet og
prøves på nytt neste kjøring — bedre å varsle sent enn å tape varselet stille.

## Ingen tom digest

En ukesmail som sier «ingen endringer» 52 ganger i året blir filtrert bort, og
da forsvinner den viktige uken 53 med den. Vi sender bare når det er noe.

Unntaket er *stillhet som i seg selv er et varsel*: har ikke cron-jobben kjørt
på lenge, er det verdt en e-post. Det er `_staleness_warning`.
"""

from __future__ import annotations

import json
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

from .brreg import ATTRIBUTION
from .store import Change, ChangeKind, Store, utc_now

__all__ = [
    "ConsoleSink",
    "NotifyReport",
    "Sink",
    "SmtpSink",
    "WebhookSink",
    "notify",
    "sink_from_env",
]


class Sink(ABC):
    """Et sted et varsel kan havne."""

    @abstractmethod
    def send(self, subject: str, body: str) -> None:
        """Send, eller reis et unntak. Stille feil er verre enn ingen varsling."""


@dataclass(slots=True)
class ConsoleSink(Sink):
    """Skriver til stdout. Standard, og det testene bruker."""

    sent: list[tuple[str, str]] | None = None

    def send(self, subject: str, body: str) -> None:
        if self.sent is None:
            self.sent = []
        self.sent.append((subject, body))
        print(f"=== {subject} ===\n{body}\n")


@dataclass(slots=True)
class SmtpSink(Sink):
    """E-post. Regnskapsbyråer lever i innboksen."""

    host: str
    port: int
    username: str
    password: str
    sender: str
    recipients: list[str]
    use_tls: bool = True
    timeout: float = 30.0

    def send(self, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message.set_content(body)

        if self.port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                self.host, self.port, timeout=self.timeout, context=context
            ) as server:
                server.login(self.username, self.password)
                server.send_message(message)
            return

        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
            if self.use_tls:
                server.starttls(context=ssl.create_default_context())
            if self.username:
                server.login(self.username, self.password)
            server.send_message(message)


@dataclass(slots=True)
class WebhookSink(Sink):
    """Slack, Teams eller hva som helst som tar imot JSON."""

    url: str
    timeout: float = 20.0

    def send(self, subject: str, body: str) -> None:
        payload = json.dumps({"text": f"*{subject}*\n```\n{body}\n```"}).encode()
        request = urllib.request.Request(
            self.url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            if response.status >= 400:
                raise urllib.error.HTTPError(
                    self.url, response.status, "webhook avviste varselet", response.headers, None
                )


@dataclass(frozen=True, slots=True)
class NotifyReport:
    urgent_sent: int = 0
    digest_sent: int = 0
    marked: int = 0
    skipped_reason: str | None = None


def sink_from_env(env: dict[str, str] | None = None) -> Sink:
    """Velg kanal ut fra miljøvariabler. Konsoll hvis ingenting er satt.

    ``RADAR_WEBHOOK_URL`` — Slack/Teams-webhook, eller
    ``RADAR_SMTP_HOST`` + ``_PORT`` + ``_USER`` + ``_PASS`` + ``_FROM`` + ``_TO``.
    """
    env = env if env is not None else dict(os.environ)

    webhook = env.get("RADAR_WEBHOOK_URL", "").strip()
    if webhook:
        return WebhookSink(webhook)

    host = env.get("RADAR_SMTP_HOST", "").strip()
    recipients = [r.strip() for r in env.get("RADAR_SMTP_TO", "").split(",") if r.strip()]
    if host and recipients:
        return SmtpSink(
            host=host,
            port=int(env.get("RADAR_SMTP_PORT", "587")),
            username=env.get("RADAR_SMTP_USER", ""),
            password=env.get("RADAR_SMTP_PASS", ""),
            sender=env.get("RADAR_SMTP_FROM", "") or env.get("RADAR_SMTP_USER", ""),
            recipients=recipients,
        )

    return ConsoleSink()


def notify(
    store: Store,
    *,
    tenant: str,
    sink: Sink | None = None,
    digest_weekday: int | None = 1,
    stale_after_hours: int = 72,
    anomaly: str | None = None,
) -> NotifyReport:
    """Send det som skal sendes, og merk det som sendt.

    ``digest_weekday`` er ISO-ukedag (1 = mandag). ``None`` slår av digesten.
    ``anomaly`` overstyrer; utelates den, spør vi lageret selv om siste kjøring
    ble forkastet. Da går det ut ett varsel om *det*, og ingen endringsvarsler.
    """
    sink = sink or sink_from_env()
    anomaly = anomaly or store.last_anomaly()

    if anomaly:
        sink.send(
            f"[radar] Kjøring forkastet — {tenant}",
            _anomaly_body(anomaly, tenant),
        )
        return NotifyReport(skipped_reason="avvik")

    stale = _staleness_warning(store, tenant, stale_after_hours)
    if stale:
        sink.send(f"[radar] Ingen ferske data — {tenant}", stale)

    report_urgent = 0
    marked = 0
    urgent = store.unnotified(tenant=tenant, kinds=ChangeKind.URGENT)
    if urgent:
        sink.send(
            f"[radar] {len(urgent)} kunder krever handling — {tenant}",
            _urgent_body(urgent, tenant),
        )
        # Merkes FØRST etter at sendingen faktisk gikk gjennom — men STRAKS
        # etter. Venter vi til etter digesten, vil en digest-feil sende de
        # samme hastevarslene på nytt i morgen. Én endring, ett varsel.
        marked += store.mark_notified(urgent)
        report_urgent = len(urgent)

    # Digesten går bare på den avtalte ukedagen, og bare hvis noe har skjedd.
    report_digest = 0
    if digest_weekday is not None and datetime.now(UTC).isoweekday() == digest_weekday:
        rest = [
            c
            for c in store.unnotified(tenant=tenant)
            if c.kind not in ChangeKind.URGENT
        ]
        if rest:
            sink.send(
                f"[radar] Ukesoppdatering — {tenant}", _digest_body(rest, store, tenant)
            )
            marked += store.mark_notified(rest)
            report_digest = len(rest)

    return NotifyReport(report_urgent, report_digest, marked)


def _urgent_body(changes: list[Change], tenant: str) -> str:
    lines = [
        f"{len(changes)} motparter hos {tenant} har fått en endring som brekker",
        "e-fakturaflyten. Fra 1. januar 2027 er det sendeplikt.",
        "",
    ]
    for change in changes:
        lines.append(f"  {change.observed_at[:10]}  {_explain(change)}")
    lines += ["", f"Generert {utc_now()}.", ATTRIBUTION]
    return "\n".join(lines)


def _digest_body(changes: list[Change], store: Store, tenant: str) -> str:
    coverage = store.coverage(tenant)
    lines = [f"Endringer for {tenant} siden forrige oppdatering:", ""]
    for change in changes:
        lines.append(f"  {change.observed_at[:10]}  {_explain(change)}")
    counts = store.summary(tenant)
    total = sum(counts.values()) or 1
    lines += ["", "Status nå:"]
    for status, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {status:26s} {count:5d}  ({count / total * 100:.0f} %)")
    lines += [
        "",
        f"Under overvåking: {coverage.watched}. "
        f"Aldri sjekket: {coverage.never_checked}. "
        f"Siste kjøring: {coverage.last_run_at or 'aldri'}.",
        "",
        f"Generert {utc_now()}.",
        ATTRIBUTION,
    ]
    return "\n".join(lines)


def _anomaly_body(anomaly: str, tenant: str) -> str:
    return "\n".join(
        [
            f"Siste kjøring for {tenant} ble forkastet av sikkerhetsventilen.",
            "",
            f"  {anomaly}",
            "",
            "Ingen data ble skrevet, og ingen endringsvarsler er sendt. Motpartene",
            "står fortsatt som utdaterte, så neste kjøring prøver dem på nytt.",
            "",
            "Sjekk om NAPTR-oppslag virker før du gjør noe annet:",
            "  efaktura-radar sjekk 986252932 --kun-dns",
            "",
            "Merk at den gamle SML-sonen sluttet å svare 31. august 2026. Er dette",
            "en migrasjonsfeil, ligger den i sml.py.",
            "",
            f"Generert {utc_now()}.",
        ]
    )


def _staleness_warning(store: Store, tenant: str, hours: int) -> str | None:
    """Stillhet er ikke det samme som at alt er i orden."""
    coverage = store.coverage(tenant)
    if coverage.last_run_at is None:
        return None
    cutoff = (
        (datetime.now(UTC) - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    )
    if coverage.last_run_at >= cutoff:
        return None
    return "\n".join(
        [
            f"Siste vellykkede kjøring for {tenant} var {coverage.last_run_at}.",
            f"Det er mer enn {hours} timer siden.",
            "",
            "Et dashboard uten ferske data ser identisk ut med et dashboard der",
            "alt er i orden. Sjekk om den planlagte kjøringen fortsatt er aktiv —",
            "GitHub deaktiverer den etter 60 dager uten aktivitet i repoet.",
            "",
            f"Generert {utc_now()}.",
        ]
    )


def _explain(change: Change) -> str:
    """Endringen sagt på norsk, ikke som en enum-verdi."""
    who = f"{change.orgnr} {change.name}".strip()
    text = {
        ChangeKind.DEREGISTERED: "kan ikke lenger motta e-faktura",
        ChangeKind.LOST_INVOICE: "er i Peppol, men støtter ikke lenger faktura",
        ChangeKind.LEFT_ELMA: "flyttet fra ELMA til en annen SMP",
        ChangeKind.JOINED_ELMA: "flyttet til ELMA",
        ChangeKind.REGISTERED: "kan nå motta e-faktura",
        ChangeKind.GAINED_INVOICE: "støtter nå faktura",
        ChangeKind.CHANGED_SMP: f"byttet SMP ({change.old_value} → {change.new_value})",
        ChangeKind.NEW_CAN_RECEIVE: "lagt til — kan motta e-faktura",
        ChangeKind.NEW_CANNOT_RECEIVE: "lagt til — kan IKKE motta e-faktura",
    }.get(change.kind, change.kind)
    return f"{who}: {text}"
