# efaktura-radar

Sjekk hvilke norske virksomheter som kan motta EHF-faktura — mot Peppol SML (DNS)
og SMP (HTTPS). Ingen API-nøkkel, ingen registrering, ingen avtale med noen.

Teknisk spike for e-fakturaplikten i **bokføringsloven § 10 annet ledd**
(lov 19. juni 2026 nr. 39), som trer i kraft **1. januar 2027**.

**Status: verifisert live 2026-07-27.** 102 enhetstester grønne, ruff og mypy
strict rene, hele kjeden kjørt ende-til-ende mot ekte DNS på 38 reelle
Oslo-selskaper.

Én ting er *ikke* live-verifisert: HTTP-kallene mot Brreg og SMP. De er
enhetstestet mot `httpx.MockTransport`, og API-kontrakten er bekreftet mot
Brregs eget endepunkt — men selve klientkallet er ikke kjørt mot nett. Kjør
`pytest -m live` én gang lokalt før du stoler på det.

---

## Kom i gang

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .

# Ett oppslag
efaktura-radar sjekk 986252932

# Hele kundelisten (CSV: orgnr i kolonne 1, navn i kolonne 2)
efaktura-radar batch eksempel/kundeliste.csv --ut resultat.csv

# Bare DNS — raskt, og belaster ingen tredjeparts SMP
efaktura-radar batch eksempel/kundeliste.csv --kun-dns --ut resultat.csv
```

```
$ efaktura-radar sjekk 986252932 --vis-utgatt
Organisasjonsnummer : 986252932
Deltaker-ID         : iso6523-actorid-upis::0192:986252932
NAPTR-vertsnavn     : JGPA45E5TP5QNMA3C3XUXV6V4ZE4LHDYW6AL2HZYU3NRLUFX3D3A.iso6523-actorid-upis.participant.sml.prod.tech.peppol.org
Utgått (CNAME)      : B-af698985e2b17102e6a02222b52f9ba9.iso6523-actorid-upis.edelivery.tech.ec.europa.eu
                      (slutter å svare 2026-08-31)
Status              : kan_motta
SMP                 : https://smp.elma-smp.no/
På ELMA             : ja
```

Overvåking — dette er det som faktisk selges:

```bash
# Les inn en klients kundeliste
efaktura-radar folg kunder.csv --db radar.db --byra "Nordvik Regnskap AS" --klient klient-1

# Én runde. Dette er kallet cron gjør.
efaktura-radar kjor --db radar.db --byra "Nordvik Regnskap AS"

# Ukesdigesten
efaktura-radar endringer --db radar.db --byra "Nordvik Regnskap AS" --dager 7
efaktura-radar status --db radar.db --byra "Nordvik Regnskap AS"
```

```bash
# Varsling: hastesaker i dag, resten i mandagsdigesten
efaktura-radar varsle --db radar.db --byra "Nordvik Regnskap AS"

# Navn og registerstatus fra Enhetsregisteret. Kjør månedlig.
efaktura-radar berik --db radar.db --byra "Nordvik Regnskap AS"

# Diffbare CSV-er ved siden av basen (skyller også WAL)
efaktura-radar eksport --db radar.db --ut-dir data
```

```
$ efaktura-radar status --db radar.db --byra "Nordvik Regnskap AS"
--- Nordvik Regnskap AS ---
  kan_motta                     31  (82 %)
  ikke_registrert                7  (18 %)

  Under overvåking : 38
  Aldri sjekket    : 0
  Siste kjøring    : 2026-07-27T21:25:24+00:00

  1 registrert utenfor ELMA — forarbeidene nevner kun ELMA:
    930327581
```

Tester:

```bash
pytest -m "not live"   # 102 tester, ingen nettverk
pytest -m live         # krever DNS
ruff check . && mypy
```

---

## Hva spiken faktisk beviste

### 1. Oppslagskjeden holder, og den er gratis

```
orgnr -> mod11 -> iso6523-actorid-upis::0192:NNNNNNNNN
      -> SHA-256 -> Base32 -> U-NAPTR-oppslag i DNS
      -> SMP-URL -> HTTPS GET ServiceGroup
      -> liste over dokumenttyper -> kan motta faktura?
```

Ingen spesifikasjon oppgir ratebegrensninger, registreringskrav eller
bruksvilkår for oppslag. Det er offentlig DNS pluss en offentlig HTTPS GET.
Digdirs eneste publiserte råd er uformelt: *«oppslag ved behov — ikkje køyre
store batch-jobbar»*.

Begge hash-algoritmene er verifisert byte for byte mot den offisielle
testvektoren i «Peppol CNAME to NAPTR Migration Process v1.0.0»
(`tests/test_sml.py`). Ryker de testene, er algoritmen feil — ikke testen.

### 2. Stikkprøve på ekte data: 82 % er allerede registrert

38 tilfeldige AS i Oslo med 5–50 ansatte, hentet fra Enhetsregisteret,
slått opp live mot Peppol SML 2026-07-27:

| | Antall | Andel |
|---|---|---|
| Registrert i Peppol | 31 | **82 %** |
| Ikke registrert | 7 | 18 % |
| — hvorav på ELMA | 30 av 31 | 97 % |
| — hvorav på annen SMP | **1 av 31** | **3 %** |

**Dette endrer salgsargumentet, og det er verdt å ta inn over seg.**

Rapport 2025/21 (Samfunnsøkonomisk analyse, for Skattedirektoratet) sier at
kun 32 % av virksomhetene som leverte skattemelding i 2023 var registrert i
ELMA. Men det tallet dekker *alle* virksomheter, inkludert enkeltpersonforetak
uten ansatte. Blant AS-er med 5–50 ansatte — altså akkurat den kundemassen et
regnskapsbyrå fakturerer — er dekningen 82 %.

Så «hvem av kundene dine kan ta imot EHF?» er ikke en krise. Svaret er stort
sett ja. Det verdifulle spørsmålet snur:

- **Hvem kan *ikke*** — de 18 % som vil brekke sendeplikt-flyten fra nyttår.
- **Hva endrer seg** — hvilke av mine 200 klienters 5 000 motparter skiftet
  status denne måneden. Det er abonnementet, og tidsserien kan ikke
  rekonstrueres i ettertid. Det er hele argumentet for å begynne å samle nå.

### 3. Det skarpeste funnet: ELMA er ikke hele Peppol

Ett av de 31 registrerte selskapene lå ikke på ELMA, men på `sml.ion-smp.net`.

Det er ikke en kuriositet. Digdir sier det rett ut i sin egen dokumentasjon:

> *«Previously, ELMA was the only PEPPOL SMP where norwegian organizations were
> registered. This is no longer the case.»*
>
> *«These datasets must not be used to check if a given norwegian organization
> is registered in PEPPOL or what documents an organization can receive.»*

Men **forarbeidene til loven navngir ELMA spesifikt**. Prop. 44 L kap. 3.4:

> «Det innebærer at bokføringspliktige virksomheter fra dette tidspunktet får
> plikt til å sende e-faktura til bokføringspliktige virksomheter **som er
> registrert i Elektronisk mottakerregister (ELMA)**, og som dermed kan motta
> e-faktura.»

Selve lovteksten nevner verken ELMA eller Peppol — § 10 annet ledd er en
ubetinget plikt, og ELMA-avgrensningen hviler på forarbeider pluss en forskrift
som ennå ikke er skrevet. En virksomhet som er fullt nåbar via en kommersiell
SMP faller altså utenfor forarbeidenes ordlyd.

Det er et uavklart juridisk punkt, og kommersielt er det det mest interessante
i hele analysen: **ingen kan i dag svare på hvilke av kundene dine som ligger
på ELMA kontra en annen SMP.** Digdir la ned søket sitt i februar 2025 og ba
folk la være å bruke datasettet. `SmlResult.on_elma` svarer på det.

### 4. Markedet: enkeltoppslag er gratis overalt, bulk finnes ikke

Gratis enkeltoppslag: [lookup.peppol.org](https://lookup.peppol.org/) (OpenPeppol),
[anskaffelser.dev/service/lookup](https://anskaffelser.dev/service/lookup/) (DFØ),
[Fikens EHF-oppslag](https://fiken.no/gratis-verktoy/ehf-oppslag),
[peppol.helger.com](https://peppol.helger.com/), Logiq, Maventa.

**Ikke bygg enda en oppslagsside.** Ingen tar betalt for det, fordi oppslag
selger fakturavolum.

Bulk-sjekk for Norge finnes derimot ikke. Belgia har
[Peppol Radar](https://e-invoice.be/peppol-radar) med CSV-opplasting,
Australia og New Zealand har tilsvarende. Norge har ingenting — Brønnøysund har
ikke EHF som felt, og ingen norsk dataleverandør (Proff Forvalt, Enin,
Purehelp, D&B) fører det som berikelse. **Og ingen selger endringsovervåking.**

Den ærlige motrisikoen: aktørene gir oppslag gratis fordi det selger
transaksjonsvolum, og hvem som helst av dem kan sende bulk i én sprint.
Forsvaret er tidsserien, ikke oppslaget.

### 5. En frist du bør kjenne: 31. august 2026

OpenPeppol har byttet både hash-algoritme og DNS-sone:

| | Gammel | Ny |
|---|---|---|
| Hash + record | MD5, CNAME | **SHA-256 → Base32, U-NAPTR** |
| Sone | `edelivery.tech.ec.europa.eu` | **`participant.sml.prod.tech.peppol.org`** |

Den gamle EC-sonen slutter å svare **31. august 2026** — fem uker fram.
`vefa-peppol` migrerte først i v4.5.0 (13. juli 2026), og Oxalis-NG 1.3.0
leverer fortsatt 4.4.0. Det ligger altså live norsk integrasjon som ryker.

Denne koden gjør NAPTR først, med CNAME som valgfri fallback
(`resolve_smp(..., legacy_fallback=True)`). Fjern fallbacken i september.

---

## Lagringsmodellen

Oppslaget er gratis og finnes overalt. **Tidsserien er produktet**, og den kan
ikke rekonstrueres i etterkant.

Vi lagrer ikke ett øyeblikksbilde per kjøring — 5 000 motparter ganger daglig er
1,8 mill. nesten identiske rader i året. I stedet SCD2: gjeldende tilstand i
`participant`, kun faktiske endringer i `change`. Tilstanden på et vilkårlig
tidspunkt er siste endring før det. `status_at(orgnr, dato)` svarer på
«var denne kunden registrert da vi fakturerte i mars?».

38 motparter, én kjøring, full historikk: **48 KB**.

### Tre regler som avgjør om noen stoler på tallene

**1. «Ingen endring» og «ikke sjekket» er ikke samme sak.** `run`-tabellen og
`participant.last_checked` skiller dem. Uten det viser dashboardet «alt i orden»
mens cron-jobben har stått i tre uker. `coverage()` returnerer `never_checked`
og `stale` eksplisitt.

**2. En regresjon må bekreftes før den logges.** Et DNS-timeout skal aldri bli
til «kunden mistet EHF-tilgang». Status som blir dårligere krever to påfølgende
like observasjoner; forbedringer logges umiddelbart. Det er den enkeltregelen
som avgjør om byrået fortsatt åpner mailen i måned tre.

**3. Feil skriver aldri en endring.** Timeout og SERVFAIL teller opp
`consecutive_errors` og logges på kjøringen. De rører ikke statusen.

Testfilen er skrevet rundt dette: de viktigste testene sjekker at endringer
**ikke** blir logget.

### Endringstyper byrået faktisk leser

| Type | Haster | Betydning |
|---|---|---|
| `avregistrert` | **ja** | Kunden kan ikke lenger motta. Sendeplikt-flyten brekker |
| `mistet_fakturastotte` | **ja** | Fortsatt i Peppol, men ikke for faktura |
| `forlot_elma` | **ja** | Nåbar, men utenfor forarbeidenes ordlyd |
| `registrert` | nei | God nyhet — nå må dere sende EHF hit |
| `byttet_smp` / `kom_til_elma` | nei | Månedsrapport |

## Arkitektur

| Modul | Ansvar |
|---|---|
| `orgnr.py` | mod11-validering og normalisering. Gratis filter før DNS |
| `sml.py` | Deltaker-ID, hash-navn, NAPTR-oppslag → SMP-URL |
| `smp.py` | ServiceGroup-henting og XML-tolkning → dokumenttyper |
| `doctypes.py` | EHF/Peppol BIS Billing 3.0-identifikatorer |
| `check.py` | Full kjede + parallell batch |
| `brreg.py` | Enhetsregisteret, batchet. NLOD 2.0 |
| `store.py` | SQLite, append-only endringslogg, dekning og historikk |
| `notify.py` | Hastevarsel og ukesdigest. SMTP, webhook eller konsoll |
| `monitor.py` | Kjøreløkka og digesten. Dette er det cron kaller |
| `cli.py` | `sjekk`, `batch`, `folg`, `kjor`, `varsle`, `berik`, `eksport`, `endringer`, `status` |

### To fallgruver koden håndterer

**Hash-inputen er kun verdidelen.** `0192:986252932`, ikke hele
`iso6523-actorid-upis::0192:986252932`. Dette er den vanligste
implementasjonsfeilen, og det finnes en test som låser det
(`test_hash_input_is_value_only_not_full_identifier`).

**ServiceGroup viser ikke gyldighetsdatoer.** Et endepunkt kan ha
`ServiceActivationDate` fram i tid eller `ServiceExpirationDate` bak seg, og de
feltene finnes bare i den detaljerte ServiceMetadata-ressursen. Det er den
eneste reelle kilden til falske positiver i steg 1. `smp.endpoint_is_active()`
dekker steg 2 når det trengs.

## Sikkerhetsventilen

Bekreftelsesregelen over beskytter mot at *én* motpart flapper. Den beskytter
ikke mot **systemsvikt**, for da er den andre observasjonen like feil som den
første.

Det er ikke hypotetisk. Den gamle SML-sonen slutter å svare 31. august 2026.
Går noe galt i den migrasjonen, returnerer hvert eneste NAPTR-oppslag NXDOMAIN
— og etter to kjøringer ville systemet «bekreftet» at samtlige kunder har mistet
EHF-tilgangen, og sendt varsel om det.

Derfor sjekkes hver kjøring **før** noe skrives: faller en uforholdsmessig andel
av dem som hadde status til «ikke registrert», forkastes hele kjøringen.

| Terskel | Standard |
|---|---|
| Andel som må falle | 10 % |
| Absolutt minimum | 5 |
| Minste utvalg før andelen teller | 20 |

Ingenting skrives, kjøringen merkes som avvik, og motpartene står fortsatt som
utdaterte — så neste kjøring prøver dem på nytt. Varslingen sender ett varsel om
*avviket*, ikke 200 om kundene.

Å tape en dags observasjoner er billig. Å sende 200 falske «kunden din kan ikke
lenger motta faktura» er ikke.

Verifisert i praksis: 29 av 29 falt samtidig → forkastet, 0 endringer skrevet,
1 varsel sendt. To ekte avregistreringer i samme base → sluppet gjennom, 1
varsel med begge.

## Varsling

**Hastesaker samme dag:** `avregistrert`, `mistet_fakturastotte`, `forlot_elma`.
Det er de tre som brekker sendeplikt-flyten.

**Resten i mandagsdigesten.** Ingen vil ha e-post fordi en kunde *fikk*
EHF-støtte.

To regler avgjør om noen fortsatt leser mailen i måned tre:

- **Én endring, ett varsel.** `change.notified_at` settes *etter* vellykket
  sending. Feiler sendingen, står endringen igjen som uvarslet og prøves på nytt
  — bedre å varsle sent enn å tape varselet stille.
- **Ingen tom digest.** 52 «ingen endringer»-mailer i året får uke 53 filtrert
  bort. Unntaket er stillhet som *i seg selv* er et varsel: har ikke jobben
  kjørt på 72 timer, går det ut en e-post om det.

Kanal velges av miljøvariabler, med konsoll som standard — riktig oppførsel før
første kunde:

| Variabel | Effekt |
|---|---|
| `RADAR_WEBHOOK_URL` | Slack/Teams. Vinner over SMTP |
| `RADAR_SMTP_HOST` + `_TO` (+ `_PORT`, `_USER`, `_PASS`, `_FROM`) | E-post |
| ingenting | Konsoll |

Halvkonfigurert SMTP (host uten mottakere) faller tilbake til konsoll i stedet
for å sende varselet ut i intet.

## Berikelse fra Enhetsregisteret

Orgnr er ofte alt en kundelisteeksport inneholder. `berik` henter navn,
organisasjonsform, næringskode, ansatte og konkursstatus.

Søkeendepunktet tar en kommaseparert liste, så 5 000 kunder blir ~50 kall i
stedet for 5 000:

```
GET /enhetsregisteret/api/enheter?organisasjonsnummer=A,B,C&size=100
```

Tre ting koden håndterer:

- **Underenheter.** En kundeliste kan inneholde avdelinger, som ikke ligger i
  `/enheter`. Vi faller tilbake til `/underenheter`.
- **Ukjente orgnr er ikke en feil.** De får `kind='ukjent'` og et tidsstempel,
  så de ikke slås opp på nytt hver måned. Byråets eget navn beholdes.
- **Berikelse kan aldri endre Peppol-status.** Det finnes en test på nettopp
  det. Navn er pynt; statusen er produktet.

Bonus: `flagged()` lister kunder som er konkurs, under avvikling eller slettet.
Ikke en Peppol-opplysning, men et byrå vil vite det, og det følger gratis med.

NLOD 2.0 tillater kommersiell bruk, men krever attribusjon ved videreformidling.
Teksten ligger i `brreg.ATTRIBUTION` og er allerede med i `format_digest`.

## Automatisering

`.github/workflows/radar.yml` kjører daglig 04:17 UTC og committer basen tilbake
til repoet. Samme mønster som `ValiantEvers.github.io` bruker for `dist/`, og
det gir gratis versjonert backup av det som faktisk er produktet.

| Når | Hva |
|---|---|
| Daglig | `kjor` — DNS-runde, ~2 000 motparter, deretter `varsle` |
| Søndag | `kjor --full` — bekrefter fakturastøtte via SMP |
| Den 1. | `berik` — Enhetsregisteret |
| Hver gang | `eksport` → `data/endringer.csv` og `data/status.csv` |

`data/*.csv` differ rent i GitHub-grensesnittet, så du ser historikken uten å
åpne basen. `.gitattributes` merker `.db` som binær, og WAL-sidefilene er
gitignorert — `eksport` skyller dem inn i hovedfila først.

**Tre ting som kan drepe innsamlingen stille:**

1. **60-dagersregelen.** GitHub deaktiverer planlagte kjøringer etter 60 dager
   uten aktivitet i repoet, og **push med `GITHUB_TOKEN` teller ikke**
   ([bekreftet i GitHubs egen dokumentasjon](https://docs.github.com/actions/managing-workflow-runs/disabling-and-enabling-a-workflow)).
   Workflowen advarer i sammendraget etter 45 dager. Varig fiks: bytt til en PAT
   i `checkout`-steget.
2. **SML-migrasjonen 31. august 2026.** Koden gjør NAPTR først, men det første
   steget i workflowen slår opp DFØ og feiler høylytt hvis DNS-veien ryker.
3. **Overlappende kjøringer.** `concurrency: radar` med
   `cancel-in-progress: false` — å avbryte midt i en SQLite-skriving er verre
   enn å vente.

`.github/workflows/ci.yml` kjører ruff, mypy og testene på hver push. Live-testene
er bevisst utenfor CI — de treffer tredjeparts infrastruktur.

### Videre

- **Pin kodelisteversjonen.** Peppol-kodelistene er dynamiske; v9.7 er datert
  2026-07-02. Ikke hardkod én gang og glem det.
- **Ikke bygg dashboard før noen har betalt.** `status` og `endringer` i
  terminalen er nok til de første samtalene med et byrå.
- **Ingen kunde har sett dette ennå.** Alt over er infrastruktur. Neste steg er
  én samtale med ett regnskapsbyrå, ikke mer kode.

---

## Rettslig grunnlag

| | |
|---|---|
| Proposisjon | Prop. 44 L (2025–2026), Finansdepartementet, 20.03.2026 |
| Innstilling | Innst. 262 L, Finanskomiteen, enstemmig, 07.05.2026 |
| Lovvedtak | Lovvedtak 52 (2025–2026), 01.06.2026 |
| Sanksjon | **19.06.2026, lov nr. 39** |
| Ikrafttredelse | **01.01.2027** for §§ 3, 10, 11, 13 — fastsatt ved kgl.res., ikke bare varslet |
| Digital bokføring | 01.01.2030 (§ 7 fjerde ledd) |
| Forskrift | Ikke gitt. Skattedirektoratets utredningsfrist **15.12.2026** |

Bokføringsloven § 10 nytt annet ledd:

> «Dokumentasjon for salg av varer og tjenester til andre bokføringspliktige
> skal utstedes i elektronisk fakturaformat, jf. § 3 nr. 3. Dokumentasjon for
> kjøp av varer og tjenester fra andre bokføringspliktige skal tilsvarende
> mottas i elektronisk fakturaformat.»

Unntak: omsetning under 50 000 kr, visse konkursbo, finanssektoren, salg til
forbrukere, kontantsalg.

**Åpne punkter:** forskriften finnes ikke ennå — format, ELMA-vilkåret, unntak
og overgangsregler ligger alle der. Mottaksplikten er dessuten i reell spenning
mellom kilder: kgl.res. lister § 10 samlet fra 01.01.2027, mens Prop. 44 L sier
mottaksplikt fra 2030 med overgangsregler som ennå ikke er skrevet.

---

## Kilder

[Prop. 44 L (2025–2026)](https://www.regjeringen.no/contentassets/c0e0271bc56c43dc82575e4df33d0f39/no/pdfs/prp202520260044000dddpdfs.pdf) ·
[Lovvedtak 52](https://www.stortinget.no/no/Saker-og-publikasjoner/Vedtak/Beslutninger/Lovvedtak/2025-2026/vedtak-202526-052/) ·
[Ikrafttredelse](https://www.regjeringen.no/no/aktuelt/nye-lovregler-om-e-fakturering-i-naringslivet-og-enkelte-andre-lovendringer-pa-finansmarkedsomradet-settes-i-kraft/id3166726/) ·
[Rapport 2025/21](https://www.regjeringen.no/contentassets/46f37b195426473bbf1fdf64d7d47344/horingsnotat-vedlegg-samfunnsokonomisk-analyse.pdf) ·
[PFUOI v4.4.0](https://docs.peppol.eu/edelivery/policies/Peppol-EDN-Policy-for-use-of-identifiers-4.4.0-2025-02-06.pdf) ·
[SML v1.3.0](https://docs.peppol.eu/edelivery/sml/Peppol-EDN-Service-Metadata-Locator-1.3.0-2025-02-06.pdf) ·
[SMP v1.4.0](https://docs.peppol.eu/edelivery/smp/Peppol-EDN-Service-Metadata-Publishing-1.4.0-2025-02-06.pdf) ·
[CNAME→NAPTR-migrasjon v1.0.0](https://docs.peppol.eu/edelivery/changelog/2025-04/Peppol%20CNAME%20to%20NAPTR%20Migration%20Process%20v1.0.0%202025-04-17.pdf) ·
[Peppol-kodelister v9.7](https://docs.peppol.eu/edelivery/codelists/index.html) ·
[SML Insourcing](https://openpeppol.atlassian.net/wiki/spaces/PTPUB/pages/5059608580/SML+Insourcing) ·
[ELMA bruksvilkår](https://samarbeid.digdir.no/elma/bruksvilkar-elma/2072) ·
[ELMA open data](https://docs.digdir.no/docs/ELMA/elma_open_data) ·
[Enhetsregisteret API](https://data.brreg.no/enhetsregisteret/api/dokumentasjon/no/index.html) ·
[NLOD 2.0](https://data.norge.no/nlod/no/2.0)

---

## Lisens

[MIT](LICENSE). Bruk det, endre det, ta det inn i noe eget — ingen betingelser utover at lisensteksten følger med.

Data fra Enhetsregisteret er lisensiert under [NLOD 2.0](https://data.norge.no/nlod/no/2.0) og krever kildeangivelse; `brreg.py` bærer attribusjonsteksten.
