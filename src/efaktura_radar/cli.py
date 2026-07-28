"""Kommandolinje for spiken.

    # Ett oppslag, full kjede
    efaktura-radar sjekk 986252932

    # En hel kundeliste (CSV med orgnr i første kolonne, navn i andre)
    efaktura-radar batch kunder.csv --ut resultat.csv

    # Bare DNS — raskt sveip, ingen belastning på tredjeparts SMP-er
    efaktura-radar batch kunder.csv --kun-dns --ut resultat.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

from .brreg import ATTRIBUTION, enrich_store
from .check import Status, check, check_many
from .doctypes import INVOICE
from .monitor import digest, format_digest, run_once
from .notify import notify, sink_from_env
from .sml import EC_SUNSET, legacy_hostname, naptr_hostname, participant_id
from .store import Store


def _read_rows(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(sample, ";,\t|")
        except csv.Error:
            dialect = csv.excel
        for record in csv.reader(handle, dialect):
            if not record or not record[0].strip():
                continue
            first = record[0].strip()
            if not any(c.isdigit() for c in first):
                continue  # hopper over overskriftsrad
            rows.append((first, record[1].strip() if len(record) > 1 else ""))
    return rows


def _cmd_sjekk(args: argparse.Namespace) -> int:
    result = check(args.orgnr, dns_only=args.kun_dns)
    print(f"Organisasjonsnummer : {result.orgnr}")
    print(f"Deltaker-ID         : {participant_id(result.orgnr)}")
    print(f"NAPTR-vertsnavn     : {naptr_hostname(result.orgnr)}")
    if args.vis_utgatt:
        print(f"Utgått (CNAME)      : {legacy_hostname(result.orgnr)}")
        print(f"                      (slutter å svare {EC_SUNSET.isoformat()})")
    print(f"Status              : {result.status}")
    if result.smp_url:
        print(f"SMP                 : {result.smp_url}")
        print(f"På ELMA             : {'ja' if result.on_elma else 'NEI — annen SMP'}")
    if result.doctype_count:
        print(f"Dokumenttyper       : {result.doctype_count}")
        print(f"Kreditnota          : {'ja' if result.can_receive_credit_note else 'nei'}")
        if result.other_capabilities:
            print(f"Øvrige prosesser    : {result.other_capabilities}")
    if result.error:
        print(f"Merknad             : {result.error}")
    return 0 if result.status == Status.CAN_RECEIVE else 1


def _cmd_batch(args: argparse.Namespace) -> int:
    rows = _read_rows(args.fil)
    if not rows:
        print(f"Fant ingen rader i {args.fil}", file=sys.stderr)
        return 2
    print(f"Sjekker {len(rows)} virksomheter…", file=sys.stderr)

    results = sorted(
        check_many(rows, dns_only=args.kun_dns, workers=args.parallell),
        key=lambda r: (r.status, r.name or r.orgnr),
    )

    fields = list(results[0].as_row().keys())
    handle = args.ut.open("w", newline="", encoding="utf-8") if args.ut else sys.stdout
    try:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
        writer.writeheader()
        for result in results:
            writer.writerow(result.as_row())
    finally:
        if args.ut:
            handle.close()

    tally = Counter(r.status for r in results)
    total = len(results)
    print("\n--- Oppsummering ---", file=sys.stderr)
    for status, count in tally.most_common():
        print(f"  {status:26s} {count:5d}  ({count / total * 100:.0f} %)", file=sys.stderr)

    blockers = tally[Status.NOT_REGISTERED] + tally[Status.REGISTERED_NO_INVOICE]
    print(
        f"\n  {blockers} av {total} kan ikke ta imot EHF-faktura i dag.",
        file=sys.stderr,
    )
    off_elma = [r for r in results if r.on_elma is False]
    if off_elma:
        print(
            f"  {len(off_elma)} ligger på en annen SMP enn ELMA — "
            "forarbeidene til bokføringsloven nevner kun ELMA.",
            file=sys.stderr,
        )
    if args.ut:
        print(f"\nSkrev {args.ut}", file=sys.stderr)
    return 0


def _cmd_folg(args: argparse.Namespace) -> int:
    """Les en kundeliste inn i overvåkningslista."""
    rows = _read_rows(args.fil)
    with Store(args.db) as store:
        count = store.import_watchlist(args.byra, rows, client_ref=args.klient)
        total = len(store.watched(args.byra))
    print(f"La til {count} motparter. {args.byra} følger nå {total} totalt.", file=sys.stderr)
    return 0


def _cmd_kjor(args: argparse.Namespace) -> int:
    """Én overvåkingsrunde. Dette er det cron kaller."""
    with Store(args.db, confirmations=args.bekreftelser) as store:
        report = run_once(
            store,
            tenant=args.byra,
            dns_only=not args.full,
            stale_after_hours=args.intervall,
            batch_size=args.antall,
            workers=args.parallell,
        )
        cov = store.coverage(args.byra)

    if report.anomaly:
        print(f"::warning::{report.anomaly}", file=sys.stderr)
        print(f"\nKjøring {report.run_id} FORKASTET.\n  {report.anomaly}", file=sys.stderr)
        return 3

    print(
        f"Kjøring {report.run_id}: {report.checked} sjekket, "
        f"{report.changed} endringer, {report.errors} feil.",
        file=sys.stderr,
    )
    print(
        f"Dekning: {cov.checked_ever}/{cov.watched} sjekket minst én gang, "
        f"{cov.never_checked} aldri.",
        file=sys.stderr,
    )
    for change in report.urgent:
        print(f"  HASTER  {change.kind:24s} {change.orgnr} {change.name}", file=sys.stderr)
    return 0


def _cmd_berik(args: argparse.Namespace) -> int:
    """Hent navn og registerstatus fra Enhetsregisteret. Kjør månedlig."""
    with Store(args.db) as store:
        report = enrich_store(
            store, tenant=args.byra, max_age_days=args.alder, limit=args.antall
        )
        flagged = store.flagged(args.byra)

    print(
        f"Berikelse: {report.found} funnet "
        f"({report.found_enhet} enheter, {report.found_underenhet} underenheter), "
        f"{report.unknown} ukjente, {report.errors} feil "
        f"av {report.requested} forespurte.",
        file=sys.stderr,
    )
    if flagged:
        print(f"\n{len(flagged)} kunder med merknad i Enhetsregisteret:", file=sys.stderr)
        for orgnr, name, reason in flagged[:20]:
            print(f"  {orgnr}  {name[:40]:42s}{reason}", file=sys.stderr)
    print(f"\n{ATTRIBUTION}", file=sys.stderr)
    return 0


def _cmd_eksport(args: argparse.Namespace) -> int:
    """Skriv diffbare CSV-er ved siden av basen. Dette er git-lesbar historikk."""
    args.ut_dir.mkdir(parents=True, exist_ok=True)
    with Store(args.db) as store:
        store.checkpoint()
        changes = store.export_changes()
        state = store.export_state()

    for name, rows, header in (
        ("endringer.csv", changes,
         ["observed_at", "orgnr", "navn", "kind", "field", "old_value", "new_value"]),
        ("status.csv", state,
         ["orgnr", "navn", "status", "on_elma", "smp_url", "doctype_count", "last_ok",
          "brreg_orgform", "brreg_ansatte", "brreg_konkurs", "brreg_kind"]),
    ):
        path = args.ut_dir / name
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=header, delimiter=";")
            writer.writeheader()
            writer.writerows(rows)
        print(f"Skrev {path} ({len(rows)} rader)", file=sys.stderr)
    return 0


def _cmd_varsle(args: argparse.Namespace) -> int:
    """Send hastevarsler og eventuell ukesdigest. Kjøres etter «kjor»."""
    sink = sink_from_env()
    with Store(args.db) as store:
        report = notify(
            store,
            tenant=args.byra,
            sink=sink,
            digest_weekday=None if args.ingen_digest else args.digest_dag,
        )
    print(
        f"Varsling via {type(sink).__name__}: {report.urgent_sent} haster, "
        f"{report.digest_sent} i digest, {report.marked} merket som sendt."
        + (f" ({report.skipped_reason})" if report.skipped_reason else ""),
        file=sys.stderr,
    )
    return 0


def _cmd_endringer(args: argparse.Namespace) -> int:
    with Store(args.db) as store:
        changes = digest(store, args.byra, days=args.dager)
    print(format_digest(changes, tenant=args.byra))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    with Store(args.db) as store:
        counts = store.summary(args.byra)
        cov = store.coverage(args.byra)
        off = store.off_elma(args.byra)

    total = sum(counts.values())
    print(f"--- {args.byra} ---")
    for status, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        share = f"({count / total * 100:.0f} %)" if total else ""
        print(f"  {status:26s} {count:5d}  {share}")
    print(f"\n  Under overvåking : {cov.watched}")
    print(f"  Aldri sjekket    : {cov.never_checked}")
    print(f"  Siste kjøring    : {cov.last_run_at or 'aldri'}")
    if off:
        print(f"\n  {len(off)} registrert utenfor ELMA — forarbeidene nevner kun ELMA:")
        for orgnr in off[:10]:
            print(f"    {orgnr}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="efaktura-radar",
        description="Sjekk hvem som kan motta EHF-faktura, mot Peppol SML og SMP.",
        epilog=f"Fakturatype det matches på: {INVOICE}",
    )
    sub = parser.add_subparsers(dest="kommando", required=True)

    one = sub.add_parser("sjekk", help="slå opp ett organisasjonsnummer")
    one.add_argument("orgnr")
    one.add_argument("--kun-dns", action="store_true", help="hopp over SMP-kallet")
    one.add_argument("--vis-utgatt", action="store_true", help="vis også gammelt CNAME-navn")
    one.set_defaults(func=_cmd_sjekk)

    many = sub.add_parser("batch", help="sjekk en CSV med kunder")
    many.add_argument("fil", type=Path)
    many.add_argument("--ut", type=Path, help="skriv resultat til CSV")
    many.add_argument("--kun-dns", action="store_true")
    many.add_argument("--parallell", type=int, default=8)
    many.set_defaults(func=_cmd_batch)

    # --- overvåking ------------------------------------------------------
    def _with_db(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("--db", type=Path, default=Path("radar.db"))
        sp.add_argument("--byra", default="standard", help="leietaker (regnskapsbyrå)")
        return sp

    follow = _with_db(sub.add_parser("folg", help="les kundeliste inn i overvåkningslista"))
    follow.add_argument("fil", type=Path)
    follow.add_argument("--klient", default="", help="byråets referanse for klienten")
    follow.set_defaults(func=_cmd_folg)

    run = _with_db(sub.add_parser("kjor", help="én overvåkingsrunde (kall denne fra cron)"))
    run.add_argument("--full", action="store_true", help="ta med SMP-oppslag, ikke bare DNS")
    run.add_argument("--intervall", type=int, default=24, help="timer før en rad er utdatert")
    run.add_argument("--antall", type=int, default=1000, help="maks per kjøring")
    run.add_argument("--parallell", type=int, default=8)
    run.add_argument(
        "--bekreftelser", type=int, default=2,
        help="observasjoner før en regresjon logges",
    )
    run.set_defaults(func=_cmd_kjor)

    rich = _with_db(sub.add_parser("berik", help="hent navn m.m. fra Enhetsregisteret"))
    rich.add_argument("--alder", type=int, default=30, help="dager før registerdata er utdatert")
    rich.add_argument("--antall", type=int, default=5000)
    rich.set_defaults(func=_cmd_berik)

    warn = _with_db(sub.add_parser("varsle", help="send hastevarsler og ukesdigest"))
    warn.add_argument("--digest-dag", type=int, default=1, help="ISO-ukedag, 1 = mandag")
    warn.add_argument("--ingen-digest", action="store_true")
    warn.set_defaults(func=_cmd_varsle)

    exp = sub.add_parser("eksport", help="skriv diffbare CSV-er for git")
    exp.add_argument("--db", type=Path, default=Path("radar.db"))
    exp.add_argument("--ut-dir", type=Path, default=Path("data"))
    exp.set_defaults(func=_cmd_eksport)

    diff = _with_db(sub.add_parser("endringer", help="endringslogg som ren tekst"))
    diff.add_argument("--dager", type=int, default=7)
    diff.set_defaults(func=_cmd_endringer)

    _with_db(sub.add_parser("status", help="fordeling og dekning")).set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
