# efaktura-radar — instruks for Claude Code-økter

Peppol/EHF-mottaksradar (Python CLI + SQLite): sjekker hvilke norske virksomheter som kan
motta EHF-faktura mot Peppol SML (DNS) og SMP (HTTPS), med tidsserie og endringsvarsling.
Teknisk spike for e-fakturaplikten i bokføringsloven § 10 annet ledd (i kraft 2027-01-01).
Les `README.md`.

## Særegenheter — avviker fra resten av porteføljen
- **HTTPS-remote** (`https://github.com/ValiantEvers/efaktura-radar.git`) — eneste repo som
  ikke står på SSH. `repos.tsv` bruker SSH-form, så friske kloner via sync-all blir SSH.
- **Radar-cronen committer selv:** `radar.yml` (GitHub Actions) kjører radaren og committer
  resultater i `data/` rett til `main` (`[skip ci]`). Derfor: **alltid `git pull --rebase`
  før push** — lokale commits må rebases over cron-commits.

## Semantikk (ikke svekk disse garantiene)
- `verified`-flagget er sant **kun** når et SMP-svar faktisk er lest, eller DNS svarte
  NXDOMAIN — aldri fra antatt mottaksstatus.
- Rene DNS-runder overskriver ikke tidligere SMP-funn (dokumenttype-felter beholdes).
- HTTP-feil er ERROR, ikke en observasjon. Hastevarsler markeres sendt straks etter
  sending, så en digest-feil ikke re-sender dem.

## Bygg / verifisering
- Python 3.13; `dnspython` + `httpx`. `python -m venv .venv` → `pip install -e .`
- `pytest -m "not live"` er standard (116 tester per 2026-07-28, ingen nettverk);
  `pytest -m live` krever ekte DNS og kjøres bevisst/lokalt. HTTP-klientene mot Brreg/SMP
  er mock-testet, ikke live-verifisert (se README).
- `ruff check .` og `mypy --strict` skal være rene. CI: `ci.yml`.
