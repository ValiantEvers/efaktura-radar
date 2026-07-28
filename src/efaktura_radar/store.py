"""Lagring: append-only endringslogg over EHF-status.

Dette er selve produktet. Oppslaget er gratis og finnes overalt — tidsserien
er det ingen har, og den kan ikke rekonstrueres i etterkant.

## Modellen

Vi lagrer **ikke** ett øyeblikksbilde per kjøring. 5 000 motparter ganger daglig =
1,8 mill. rader i året, nesten alle identiske. I stedet brukes SCD2: gjeldende
tilstand i `participant`, og kun faktiske endringer i `change`. Tilstanden på
et vilkårlig tidspunkt T er siste endring <= T.

## Tre regler som avgjør om produktet er til å stole på

**1. «Ingen endring» og «ikke sjekket» er ikke samme sak.**
`run`-tabellen og `participant.last_checked` skiller dem. Uten det kan et
dashboard vise «alt i orden» når cron-jobben egentlig har stått i tre uker.

**2. En regresjon må bekreftes før den logges.**
Et DNS-timeout skal aldri bli til «kunden mistet EHF-tilgang». Falske alarmer
ødelegger tilliten til et overvåkingsprodukt langt raskere enn tapte varsler.
Status som blir *dårligere* krever derfor ``confirmations`` påfølgende like
observasjoner (standard 2). Forbedringer logges umiddelbart — de er ufarlige.
Observasjonene må dessuten kunne *se* det de bekrefter: en dns-runde ser ikke
dokumenttyper, så et ubekreftet «kan_motta» verken nullstiller eller bekrefter
en ventende ``registrert_uten_faktura`` fra SMP-runden (``CheckResult.verified``).

**3. Feil skriver aldri en endring.**
Timeout, SERVFAIL og HTTP-feil teller opp `consecutive_errors` og logges på
kjøringen. De rører ikke statusen.

## Kilder og lisens

Berikelse fra Enhetsregisteret lagres med eget tidsstempel — annen kadens,
annen kilde, og NLOD 2.0 krever attribusjon ved videreformidling:
«Inneholder data under norsk lisens for offentlige data (NLOD) tilgjengeliggjort
av Brønnøysundregistrene.»
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from .check import CheckResult, Status
from .orgnr import InvalidOrgnr, normalise

__all__ = ["Change", "ChangeKind", "Coverage", "Store", "utc_now"]

#: Hvor «bra» en status er. Fall i rang = regresjon = krever bekreftelse.
_SEVERITY: dict[str, int] = {
    Status.CAN_RECEIVE: 2,
    Status.REGISTERED_NO_INVOICE: 1,
    Status.NOT_REGISTERED: 0,
}


class ChangeKind:
    """Endringstyper, slik et regnskapsbyrå faktisk leser dem."""

    NEW_CAN_RECEIVE = "ny_kan_motta"
    NEW_CANNOT_RECEIVE = "ny_kan_ikke_motta"
    REGISTERED = "registrert"
    DEREGISTERED = "avregistrert"
    LOST_INVOICE = "mistet_fakturastotte"
    GAINED_INVOICE = "fikk_fakturastotte"
    CHANGED_SMP = "byttet_smp"
    LEFT_ELMA = "forlot_elma"
    JOINED_ELMA = "kom_til_elma"

    #: Endringer som bør utløse varsel. De øvrige hører hjemme i en månedsrapport.
    URGENT = frozenset({DEREGISTERED, LOST_INVOICE, LEFT_ELMA})


@dataclass(frozen=True, slots=True)
class Change:
    orgnr: str
    observed_at: str
    kind: str
    field: str
    old_value: str | None
    new_value: str | None
    name: str = ""
    id: int = 0
    notified_at: str | None = None

    @property
    def is_urgent(self) -> bool:
        return self.kind in ChangeKind.URGENT


@dataclass(frozen=True, slots=True)
class Coverage:
    """Hvor ferske dataene faktisk er. Uten dette lyver dashboardet."""

    watched: int
    checked_ever: int
    stale: int
    never_checked: int
    last_run_at: str | None


def utc_now() -> str:
    """ISO 8601 i UTC, sekundoppløsning. Én tidsrepresentasjon i hele basen."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


_SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS run (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    mode        TEXT NOT NULL,
    n_checked   INTEGER NOT NULL DEFAULT 0,
    n_changed   INTEGER NOT NULL DEFAULT 0,
    n_errors    INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS participant (
    orgnr                   TEXT PRIMARY KEY,
    first_seen              TEXT NOT NULL,
    last_checked            TEXT,
    last_ok                 TEXT,
    status                  TEXT,
    smp_url                 TEXT,
    on_elma                 INTEGER,
    doctype_count           INTEGER NOT NULL DEFAULT 0,
    can_receive_credit_note INTEGER NOT NULL DEFAULT 0,
    pending_status          TEXT,
    pending_count           INTEGER NOT NULL DEFAULT 0,
    consecutive_errors      INTEGER NOT NULL DEFAULT 0,
    brreg_name              TEXT,
    brreg_orgform           TEXT,
    brreg_naeringskode      TEXT,
    brreg_ansatte           INTEGER,
    brreg_konkurs           INTEGER,
    brreg_checked           TEXT
);

CREATE TABLE IF NOT EXISTS change (
    id          INTEGER PRIMARY KEY,
    orgnr       TEXT NOT NULL REFERENCES participant(orgnr),
    run_id      INTEGER REFERENCES run(id),
    observed_at TEXT NOT NULL,
    kind        TEXT NOT NULL,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT
);

CREATE TABLE IF NOT EXISTS watch (
    id         INTEGER PRIMARY KEY,
    tenant     TEXT NOT NULL,
    client_ref TEXT NOT NULL DEFAULT '',
    orgnr      TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    added_at   TEXT NOT NULL,
    removed_at TEXT,
    UNIQUE (tenant, client_ref, orgnr)
);

CREATE INDEX IF NOT EXISTS ix_change_orgnr_time ON change (orgnr, observed_at);
CREATE INDEX IF NOT EXISTS ix_change_time       ON change (observed_at);
CREATE INDEX IF NOT EXISTS ix_watch_tenant      ON watch (tenant, orgnr)
    WHERE removed_at IS NULL;
"""

#: Kolonner lagt til etter første versjon. Kjøres idempotent ved oppstart.
#: Legg nye felter her — aldri i _SCHEMA alene, da får eksisterende baser dem ikke.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("participant", "brreg_under_avvikling", "INTEGER"),
    ("participant", "brreg_slettedato", "TEXT"),
    ("participant", "brreg_kind", "TEXT"),  # 'enhet' | 'underenhet' | 'ukjent'
    ("change", "notified_at", "TEXT"),
    ("run", "anomaly", "INTEGER NOT NULL DEFAULT 0"),
)


class Store:
    """SQLite-lag. Ingen ORM — skjemaet er lite og spørringene er poenget."""

    def __init__(self, path: Path | str = ":memory:", *, confirmations: int = 2) -> None:
        """``confirmations``: antall påfølgende like observasjoner før en
        regresjon logges. 1 slår av bekreftelseskravet."""
        self.path = str(path)
        self.confirmations = max(1, confirmations)
        # sqlite3 lager fila, men ikke mappa over den. Uten dette feiler aller
        # første cron-kjøring med «unable to open database file», fordi data/
        # ikke finnes i et ferskt utsjekk.
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Legg til kolonner som mangler.

        ``CREATE TABLE IF NOT EXISTS`` rører ikke en tabell som allerede finnes,
        så nye felter må legges til eksplisitt. Basen skal leve i årevis og
        historikken kan ikke gjenskapes — den må aldri kastes og bygges på nytt.
        """
        for table, column, decl in _ADDED_COLUMNS:
            existing = {
                r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- watchlist

    def watch(
        self, tenant: str, orgnr: str, *, client_ref: str = "", label: str = ""
    ) -> None:
        """Legg en motpart under overvåking for en leietaker (et byrå)."""
        self.conn.execute(
            "INSERT INTO watch (tenant, client_ref, orgnr, label, added_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (tenant, client_ref, orgnr) DO UPDATE"
            " SET removed_at = NULL, label = excluded.label",
            (tenant, client_ref, orgnr, label, utc_now()),
        )
        self.conn.commit()

    def unwatch(self, tenant: str, orgnr: str, *, client_ref: str = "") -> None:
        """Myk sletting — historikken beholdes."""
        self.conn.execute(
            "UPDATE watch SET removed_at = ?"
            " WHERE tenant = ? AND client_ref = ? AND orgnr = ? AND removed_at IS NULL",
            (utc_now(), tenant, client_ref, orgnr),
        )
        self.conn.commit()

    def watched(self, tenant: str | None = None) -> list[str]:
        """Distinkte organisasjonsnummer under aktiv overvåking."""
        sql = "SELECT DISTINCT orgnr FROM watch WHERE removed_at IS NULL"
        params: tuple[object, ...] = ()
        if tenant is not None:
            sql += " AND tenant = ?"
            params = (tenant,)
        return [r["orgnr"] for r in self.conn.execute(sql + " ORDER BY orgnr", params)]

    # ---------------------------------------------------------------------- run

    def start_run(self, mode: str, note: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO run (started_at, mode, note) VALUES (?, ?, ?)",
            (utc_now(), mode, note),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, *, checked: int, changed: int, errors: int) -> None:
        self.conn.execute(
            "UPDATE run SET finished_at = ?, n_checked = ?, n_changed = ?, n_errors = ?"
            " WHERE id = ?",
            (utc_now(), checked, changed, errors, run_id),
        )
        self.conn.commit()

    # -------------------------------------------------------------- observasjon

    def record(self, result: CheckResult, *, run_id: int | None = None) -> list[Change]:
        """Registrer én observasjon og returner endringene den utløste.

        Kalles én gang per motpart per kjøring. Tom liste er det normale.
        """
        now = utc_now()
        row = self.conn.execute(
            "SELECT * FROM participant WHERE orgnr = ?", (result.orgnr,)
        ).fetchone()

        if result.status in (Status.ERROR, Status.INVALID_ORGNR):
            self._record_error(result, row, now)
            return []

        if row is None:
            return self._insert_new(result, now, run_id)
        return self._update_existing(result, row, now, run_id)

    def _record_error(self, result: CheckResult, row: sqlite3.Row | None, now: str) -> None:
        """Feil rører aldri statusen — bare tellere. Se regel 3 i modulteksten."""
        if row is None:
            self.conn.execute(
                "INSERT INTO participant (orgnr, first_seen, last_checked,"
                " consecutive_errors) VALUES (?, ?, ?, 1)",
                (result.orgnr, now, now),
            )
        else:
            self.conn.execute(
                "UPDATE participant SET last_checked = ?,"
                " consecutive_errors = consecutive_errors + 1 WHERE orgnr = ?",
                (now, result.orgnr),
            )
        self.conn.commit()

    def _insert_new(self, r: CheckResult, now: str, run_id: int | None) -> list[Change]:
        self.conn.execute(
            "INSERT INTO participant (orgnr, first_seen, last_checked, last_ok, status,"
            " smp_url, on_elma, doctype_count, can_receive_credit_note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r.orgnr, now, now, now, r.status, r.smp_url,
                _bool(r.on_elma), r.doctype_count, int(r.can_receive_credit_note),
            ),
        )
        kind = (
            ChangeKind.NEW_CAN_RECEIVE
            if r.status == Status.CAN_RECEIVE
            else ChangeKind.NEW_CANNOT_RECEIVE
        )
        change = Change(r.orgnr, now, kind, "status", None, r.status, r.name)
        self._write_change(change, run_id)
        self.conn.commit()
        return [change]

    def _update_existing(
        self, r: CheckResult, row: sqlite3.Row, now: str, run_id: int | None
    ) -> list[Change]:
        old_status: str | None = row["status"]
        changes: list[Change] = []

        # En dns-runde ser ikke dokumenttyper. Et ubekreftet «kan_motta» kan
        # derfor ikke motbevise at fakturastøtten mangler — behold SMP-rundens
        # funn til en ny SMP-runde sier noe annet. Uten dette nullstiller
        # mandagens dns-runde søndagens ventende regresjon, og
        # «mistet_fakturastotte» kan aldri bekreftes.
        status = r.status
        if (
            not r.verified
            and status == Status.CAN_RECEIVE
            and old_status == Status.REGISTERED_NO_INVOICE
        ):
            status = Status.REGISTERED_NO_INVOICE

        if old_status is None:
            # Raden oppsto via en feil eller berikelse og har aldri hatt status.
            # Dette er den reelle førsteobservasjonen — uten en logget endring
            # mangler tidsserien sitt ankerpunkt, og status_at() svarer None.
            kind = (
                ChangeKind.NEW_CAN_RECEIVE
                if status == Status.CAN_RECEIVE
                else ChangeKind.NEW_CANNOT_RECEIVE
            )
            changes.append(Change(r.orgnr, now, kind, "status", None, status, r.name))
        elif status != old_status:
            if _is_regression(old_status, status):
                # Regresjon: krev bekreftelse. Se regel 2 i modulteksten.
                pending = row["pending_count"] + 1 if row["pending_status"] == status else 1
                if pending < self.confirmations:
                    self.conn.execute(
                        "UPDATE participant SET last_checked = ?, last_ok = ?,"
                        " pending_status = ?, pending_count = ?, consecutive_errors = 0"
                        " WHERE orgnr = ?",
                        (now, now, status, pending, r.orgnr),
                    )
                    self.conn.commit()
                    return []
            changes.append(
                Change(
                    r.orgnr, now, _classify(old_status, status),
                    "status", old_status, status, r.name,
                )
            )

        # SMP- og ELMA-endringer er uavhengige av status og krever ingen bekreftelse.
        if status != Status.NOT_REGISTERED and old_status != Status.NOT_REGISTERED:
            if r.smp_url and row["smp_url"] and r.smp_url != row["smp_url"]:
                changes.append(
                    Change(
                        r.orgnr, now, ChangeKind.CHANGED_SMP,
                        "smp_url", row["smp_url"], r.smp_url, r.name,
                    )
                )
            old_elma, new_elma = row["on_elma"], _bool(r.on_elma)
            if old_elma is not None and new_elma is not None and old_elma != new_elma:
                changes.append(
                    Change(
                        r.orgnr, now,
                        ChangeKind.JOINED_ELMA if new_elma else ChangeKind.LEFT_ELMA,
                        "on_elma", _elma_label(old_elma), _elma_label(new_elma), r.name,
                    )
                )

        # En ventende SMP-regresjon står til en SMP-runde avgjør den; en
        # dns-runde kan verken bekrefte eller avkrefte manglende fakturastøtte.
        keep_pending = (
            not r.verified
            and row["pending_status"] == Status.REGISTERED_NO_INVOICE
            and status == Status.CAN_RECEIVE
        )
        # Dokumenttype-feltene kommer fra SMP — en dns-runde vet ingenting om
        # dem og skal ikke nullstille det SMP-runden fant.
        doctypes = r.doctype_count if r.verified else row["doctype_count"]
        credit = int(r.can_receive_credit_note) if r.verified else row["can_receive_credit_note"]

        self.conn.execute(
            "UPDATE participant SET last_checked = ?, last_ok = ?, status = ?,"
            " smp_url = ?, on_elma = ?, doctype_count = ?, can_receive_credit_note = ?,"
            " pending_status = ?, pending_count = ?, consecutive_errors = 0"
            " WHERE orgnr = ?",
            (
                now, now, status, r.smp_url, _bool(r.on_elma), doctypes, credit,
                row["pending_status"] if keep_pending else None,
                row["pending_count"] if keep_pending else 0,
                r.orgnr,
            ),
        )
        for change in changes:
            self._write_change(change, run_id)
        self.conn.commit()
        return changes

    def _write_change(self, change: Change, run_id: int | None) -> None:
        self.conn.execute(
            "INSERT INTO change (orgnr, run_id, observed_at, kind, field, old_value,"
            " new_value) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                change.orgnr, run_id, change.observed_at, change.kind,
                change.field, change.old_value, change.new_value,
            ),
        )

    # ------------------------------------------------------------- berikelse

    def enrich(self, orgnr: str, brreg: dict[str, Any], *, kind: str = "enhet") -> None:
        """Lagre Enhetsregister-felter. Egen kadens, eget tidsstempel.

        Krever attribusjon ved videreformidling — NLOD 2.0 § 5.

        ``kind='ukjent'`` brukes når orgnr ikke finnes i registeret i det hele
        tatt. Da settes bare tidsstempelet, så vi ikke slår opp igjen i tide og
        utide — men navnet fra byråets kundeliste beholdes.
        """
        self.conn.execute(
            "INSERT INTO participant (orgnr, first_seen, brreg_checked, brreg_kind)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (orgnr) DO NOTHING",
            (orgnr, utc_now(), utc_now(), kind),
        )
        if kind == "ukjent":
            self.conn.execute(
                "UPDATE participant SET brreg_checked = ?, brreg_kind = ? WHERE orgnr = ?",
                (utc_now(), kind, orgnr),
            )
            self.conn.commit()
            return

        self.conn.execute(
            "UPDATE participant SET brreg_name = ?, brreg_orgform = ?,"
            " brreg_naeringskode = ?, brreg_ansatte = ?, brreg_konkurs = ?,"
            " brreg_under_avvikling = ?, brreg_slettedato = ?, brreg_kind = ?,"
            " brreg_checked = ? WHERE orgnr = ?",
            (
                brreg.get("navn"),
                (brreg.get("organisasjonsform") or {}).get("kode"),
                (brreg.get("naeringskode1") or {}).get("kode"),
                brreg.get("antallAnsatte"),
                int(bool(brreg.get("konkurs"))),
                int(bool(brreg.get("underAvvikling"))),
                brreg.get("slettedato"),
                kind,
                utc_now(),
                orgnr,
            ),
        )
        self.conn.commit()

    def needs_enrichment(
        self, tenant: str | None = None, *, before: str, limit: int = 5_000
    ) -> list[str]:
        """Hvem mangler eller har utdatert Brreg-data?

        Enhetsregisteret endrer seg langsomt. Månedlig er rikelig — og navn er
        pynt, ikke produktet, så dette skal aldri stjele budsjett fra
        statussjekken.
        """
        watched = self.watched(tenant)
        if not watched:
            return []
        marks = ",".join("?" * len(watched))
        rows = self.conn.execute(
            f"SELECT orgnr, brreg_checked FROM participant WHERE orgnr IN ({marks})",
            watched,
        )
        checked: dict[str, str | None] = {r["orgnr"]: r["brreg_checked"] for r in rows}
        never = [o for o in watched if checked.get(o) is None]
        stale = [o for o in watched if (t := checked.get(o)) is not None and t < before]
        return (never + stale)[:limit]

    def flagged(self, tenant: str | None = None) -> list[tuple[str, str, str]]:
        """Kunder som er konkurs, under avvikling eller slettet.

        Ikke en Peppol-opplysning, men et byrå vil vite det — og det følger
        gratis med berikelsen.
        """
        sql = (
            "SELECT orgnr, COALESCE(brreg_name, '') AS name, brreg_konkurs,"
            " brreg_under_avvikling, brreg_slettedato FROM participant"
            " WHERE brreg_konkurs = 1 OR brreg_under_avvikling = 1"
            " OR brreg_slettedato IS NOT NULL"
        )
        params: tuple[object, ...] = ()
        if tenant is not None:
            sql += (
                " AND orgnr IN (SELECT orgnr FROM watch"
                " WHERE tenant = ? AND removed_at IS NULL)"
            )
            params = (tenant,)
        out: list[tuple[str, str, str]] = []
        for r in self.conn.execute(sql + " ORDER BY orgnr", params):
            if r["brreg_slettedato"]:
                reason = f"slettet {r['brreg_slettedato']}"
            elif r["brreg_konkurs"]:
                reason = "konkurs"
            else:
                reason = "under avvikling"
            out.append((r["orgnr"], r["name"], reason))
        return out

    # ------------------------------------------------------------- spørringer

    def changes_since(
        self, since: str, *, tenant: str | None = None, urgent_only: bool = False
    ) -> list[Change]:
        """Endringsloggen for en periode. Dette er digesten byrået får."""
        # Navn: Enhetsregisteret er mest pålitelig, men før berikelsen har kjørt
        # er byråets egen etikett fra kundelista det eneste vi har.
        sql = [
            "SELECT c.id, c.orgnr, c.observed_at, c.kind, c.field, c.old_value,",
            "       c.new_value, c.notified_at,",
            "       COALESCE(NULLIF(p.brreg_name, ''), NULLIF(w.label, ''), '') AS name",
            "FROM change c",
            "JOIN participant p ON p.orgnr = c.orgnr",
        ]
        params: list[object] = []
        sql.append(_label_join(tenant, params))
        sql.append("WHERE c.observed_at >= ?")
        params.append(since)
        if tenant is not None:
            sql.append(
                "AND c.orgnr IN (SELECT orgnr FROM watch"
                " WHERE tenant = ? AND removed_at IS NULL)"
            )
            params.append(tenant)
        if urgent_only:
            marks = ",".join("?" * len(ChangeKind.URGENT))
            sql.append(f"AND c.kind IN ({marks})")
            params.extend(sorted(ChangeKind.URGENT))
        sql.append("ORDER BY c.observed_at DESC, c.orgnr")
        return [
            _row_to_change(r) for r in self.conn.execute(" ".join(sql), params)
        ]

    def status_at(self, orgnr: str, when: str) -> str | None:
        """Hvilken status hadde denne virksomheten på et gitt tidspunkt?

        Det er dette SCD2 kjøper deg, og hele grunnen til å begynne å samle nå.
        """
        row = self.conn.execute(
            "SELECT new_value FROM change WHERE orgnr = ? AND field = 'status'"
            " AND observed_at <= ? ORDER BY observed_at DESC, id DESC LIMIT 1",
            (orgnr, when),
        ).fetchone()
        return None if row is None else str(row["new_value"])

    def summary(self, tenant: str | None = None) -> dict[str, int]:
        """Fordelingen byrået ser på forsiden."""
        sql = (
            "SELECT p.status, COUNT(*) AS n FROM participant p"
            " WHERE p.status IS NOT NULL"
        )
        params: tuple[object, ...] = ()
        if tenant is not None:
            sql += (
                " AND p.orgnr IN (SELECT orgnr FROM watch"
                " WHERE tenant = ? AND removed_at IS NULL)"
            )
            params = (tenant,)
        return {r["status"]: r["n"] for r in self.conn.execute(sql + " GROUP BY p.status", params)}

    def off_elma(self, tenant: str | None = None) -> list[str]:
        """Registrert, men ikke på ELMA — der forarbeidenes ordlyd blir uklar."""
        sql = "SELECT orgnr FROM participant WHERE on_elma = 0 AND status IS NOT NULL"
        params: tuple[object, ...] = ()
        if tenant is not None:
            sql += (
                " AND orgnr IN (SELECT orgnr FROM watch"
                " WHERE tenant = ? AND removed_at IS NULL)"
            )
            params = (tenant,)
        return [r["orgnr"] for r in self.conn.execute(sql + " ORDER BY orgnr", params)]

    def coverage(self, tenant: str | None = None, *, stale_before: str | None = None) -> Coverage:
        """Hvor ferske er dataene? Se regel 1 i modulteksten."""
        watched = self.watched(tenant)
        if not watched:
            return Coverage(0, 0, 0, 0, self._last_run_at())
        marks = ",".join("?" * len(watched))
        rows = self.conn.execute(
            f"SELECT orgnr, last_ok FROM participant WHERE orgnr IN ({marks})",
            watched,
        ).fetchall()
        seen = {r["orgnr"]: r["last_ok"] for r in rows}
        checked = sum(1 for v in seen.values() if v)
        stale = 0
        if stale_before is not None:
            stale = sum(1 for v in seen.values() if v and v < stale_before)
        return Coverage(
            watched=len(watched),
            checked_ever=checked,
            stale=stale,
            never_checked=len(watched) - checked,
            last_run_at=self._last_run_at(),
        )

    def _last_run_at(self) -> str | None:
        row = self.conn.execute(
            "SELECT finished_at FROM run WHERE finished_at IS NOT NULL"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else str(row["finished_at"])

    def due(self, tenant: str | None = None, *, before: str, limit: int = 10_000) -> list[str]:
        """Hvem står for tur? Aldri sjekket først, deretter eldst.

        Lar cron-jobben spre 5 000 motparter utover uka i stedet for å slå
        mot samme SMP i én støt.
        """
        watched = self.watched(tenant)
        if not watched:
            return []
        marks = ",".join("?" * len(watched))
        known: dict[str, str | None] = {
            r["orgnr"]: r["last_checked"]
            for r in self.conn.execute(
                f"SELECT orgnr, last_checked FROM participant WHERE orgnr IN ({marks})",
                watched,
            )
        }
        never = [o for o in watched if known.get(o) is None]
        stale = sorted(
            (o for o in watched if (t := known.get(o)) is not None and t < before),
            key=lambda o: str(known[o]),
        )
        return (never + stale)[:limit]

    def unnotified(
        self, *, tenant: str | None = None, kinds: frozenset[str] | None = None
    ) -> list[Change]:
        """Endringer som ennå ikke er varslet om.

        Uten dette sendes samme «kunden mistet EHF-tilgang» hver eneste natt
        til noen skrur av varslene. Én endring, ett varsel.
        """
        sql = [
            "SELECT c.id, c.orgnr, c.observed_at, c.kind, c.field, c.old_value,",
            "       c.new_value, c.notified_at,",
            "       COALESCE(NULLIF(p.brreg_name, ''), NULLIF(w.label, ''), '') AS name",
            "FROM change c",
            "JOIN participant p ON p.orgnr = c.orgnr",
        ]
        params: list[object] = []
        sql.append(_label_join(tenant, params))
        sql.append("WHERE c.notified_at IS NULL")
        if tenant is not None:
            sql.append(
                "AND c.orgnr IN (SELECT orgnr FROM watch"
                " WHERE tenant = ? AND removed_at IS NULL)"
            )
            params.append(tenant)
        if kinds:
            marks = ",".join("?" * len(kinds))
            sql.append(f"AND c.kind IN ({marks})")
            params.extend(sorted(kinds))
        sql.append("ORDER BY c.observed_at, c.id")
        return [_row_to_change(r) for r in self.conn.execute(" ".join(sql), params)]

    def mark_notified(self, changes: Iterable[Change]) -> int:
        """Merk endringer som varslet. Kall dette *etter* vellykket sending."""
        ids = [c.id for c in changes if c.id]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        now = utc_now()
        self.conn.execute(
            f"UPDATE change SET notified_at = ? WHERE id IN ({marks})",
            [now, *ids],
        )
        self.conn.commit()
        return len(ids)

    def last_anomaly(self) -> str | None:
        """Ble siste fullførte kjøring forkastet av sikkerhetsventilen?

        Lar varslingen finne det ut selv, i stedet for at det må tres gjennom
        shellet fra «kjor» til «varsle».
        """
        row = self.conn.execute(
            "SELECT anomaly, note FROM run WHERE finished_at IS NOT NULL"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None or not row["anomaly"]:
            return None
        return str(row["note"] or "Kjøringen ble forkastet.")

    def flag_anomaly(self, run_id: int, note: str) -> None:
        """Merk en kjøring som avvist av sikkerhetsventilen."""
        self.conn.execute(
            "UPDATE run SET anomaly = 1, note = ? WHERE id = ?", (note, run_id)
        )
        self.conn.commit()

    def current_statuses(self, orgnrs: Iterable[str]) -> dict[str, str | None]:
        """Gjeldende status for et sett motparter. Brukes av sikkerhetsventilen."""
        wanted = list(orgnrs)
        if not wanted:
            return {}
        marks = ",".join("?" * len(wanted))
        return {
            r["orgnr"]: r["status"]
            for r in self.conn.execute(
                f"SELECT orgnr, status FROM participant WHERE orgnr IN ({marks})",
                wanted,
            )
        }

    def checkpoint(self) -> None:
        """Skyll WAL inn i hovedfila.

        Uten dette ligger ferske skrivinger i `-wal`-sidefila, og en base som
        committes til git blir ufullstendig. Kalles før eksport.
        """
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.commit()

    def export_changes(self) -> list[dict[str, object]]:
        """Hele endringsloggen, eldst først — beregnet på en CSV i git.

        Rekkefølgen er stigende med vilje: nye rader legges til på slutten, så
        diffen mellom to kjøringer viser nøyaktig hva som skjedde.
        """
        rows = self.conn.execute(
            "SELECT c.observed_at, c.orgnr, COALESCE(p.brreg_name, '') AS navn,"
            " c.kind, c.field, c.old_value, c.new_value"
            " FROM change c JOIN participant p ON p.orgnr = c.orgnr"
            " ORDER BY c.observed_at, c.id"
        )
        return [dict(r) for r in rows]

    def export_state(self) -> list[dict[str, object]]:
        """Gjeldende tilstand, sortert på orgnr — stabil diff mellom kjøringer."""
        rows = self.conn.execute(
            "SELECT orgnr, COALESCE(brreg_name, '') AS navn, status, on_elma,"
            " smp_url, doctype_count, last_ok, brreg_orgform, brreg_ansatte,"
            " brreg_konkurs, brreg_kind"
            " FROM participant WHERE status IS NOT NULL ORDER BY orgnr"
        )
        return [dict(r) for r in rows]

    def import_watchlist(
        self, tenant: str, rows: Iterable[Sequence[str]], *, client_ref: str = ""
    ) -> tuple[int, list[str]]:
        """Bulk-innlesing av (orgnr, navn) — typisk en kundelisteeksport.

        Orgnr normaliseres her: «986 252 932» og «NO986252932MVA» er normalen i
        eksporter fra regnskapssystemer. Uten normalisering havner råstrengen i
        watch-tabellen mens observasjonene lagres på normalisert orgnr — da
        bommer tenant-filteret stille på alle endringer, og motparten regnes
        som «aldri sjekket» i hver eneste kjøring.

        Returnerer (antall lagt til, rader med ugyldig orgnr).
        """
        count = 0
        invalid: list[str] = []
        for row in rows:
            if not row:
                continue
            try:
                orgnr = normalise(row[0])
            except InvalidOrgnr:
                invalid.append(row[0])
                continue
            self.watch(
                tenant, orgnr, client_ref=client_ref, label=row[1] if len(row) > 1 else ""
            )
            count += 1
        return count, invalid


def _label_join(tenant: str | None, params: list[object]) -> str:
    """LEFT JOIN som henter byråets egen etikett — og *bare* byråets egen.

    Uten tenant-filteret her kunne byrå A fått byrå Bs interne merkelapp på en
    felles motpart inn i sin digest. Med ``tenant=None`` (adminvisning) hentes
    en vilkårlig etikett, som før.
    """
    scope = ""
    if tenant is not None:
        scope = " AND tenant = ?"
        params.append(tenant)
    return (
        "LEFT JOIN (SELECT orgnr, MIN(label) AS label FROM watch"
        f" WHERE removed_at IS NULL AND label <> ''{scope}"
        " GROUP BY orgnr) w ON w.orgnr = c.orgnr"
    )


def _row_to_change(r: sqlite3.Row) -> Change:
    keys = r.keys()
    return Change(
        orgnr=r["orgnr"],
        observed_at=r["observed_at"],
        kind=r["kind"],
        field=r["field"],
        old_value=r["old_value"],
        new_value=r["new_value"],
        name=r["name"] if "name" in keys else "",
        id=r["id"] if "id" in keys else 0,
        notified_at=r["notified_at"] if "notified_at" in keys else None,
    )


def _bool(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _elma_label(value: int | None) -> str:
    return "ELMA" if value else "annen SMP"


def _is_regression(old: str, new: str) -> bool:
    return _SEVERITY.get(new, 0) < _SEVERITY.get(old, 0)


def _classify(old: str, new: str) -> str:
    if new == Status.NOT_REGISTERED:
        return ChangeKind.DEREGISTERED
    if old == Status.NOT_REGISTERED:
        return ChangeKind.REGISTERED
    if new == Status.REGISTERED_NO_INVOICE:
        return ChangeKind.LOST_INVOICE
    if old == Status.REGISTERED_NO_INVOICE and new == Status.CAN_RECEIVE:
        return ChangeKind.GAINED_INVOICE
    return ChangeKind.REGISTERED
