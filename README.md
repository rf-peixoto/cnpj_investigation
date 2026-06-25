# cnpjscope — CNPJ correlation & fraud-analysis platform

A self-hosted Flask app for investigating clusters of Brazilian companies.
You group CNPJs into **campaigns**, the app pulls each record from the free
[opencnpj.org](https://opencnpj.org) API, normalizes it into indexed tables,
then surfaces every correlation it can find and scores each company for fraud
risk with a transparent, per-signal **evidence trail**. It plots companies on a
map, finds connection paths between any two CNPJs, and lets you search the whole
pool across campaigns.

---

## Quick start

```bash
cd cnpj_platform
python3 -m venv .venv && source .venv/bin/activate      # optional but recommended
pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000>.

A SQLite file `cnpj_platform.db` is created next to the code (override with the
`CNPJ_DB` env var). Uploaded evidence images go to `./uploads` (override with
`CNPJ_UPLOADS`). Fetched records are cached, so the same CNPJ is never pulled
twice across campaigns.

Environment variables: `CNPJ_DB` (database path), `CNPJ_UPLOADS` (attachment
directory), `CNPJ_SECRET` (Flask session secret; a random one is used if unset).

---

## What's new in this version

**1 · Real CNPJ validation.** `cnpj_utils.py` validates both modulo-11 check
digits, so malformed numbers are rejected at ingest and never waste an API call.

**2 · Normalized persistence.** Phones, e-mails, CNAEs and partners are exploded
into separate indexed tables (`phones`, `emails`, `cnaes`, `partners`) in
addition to the raw JSON, which powers fast correlation and richer search
(by CEP, e-mail domain, phone prefix, any CNAE).

**3 · Evidence-first scoring.** Every risk point is backed by an evidence record
— `{signal, weight, confidence, matched entities, source, timestamp}`. Hover any
evidence pill in the companies table to see exactly why a company scored what it
did. Risk flags are treated as leads, not proof.

**4 · Many more correlations:** shared CEP; same street+number+CEP (fuzzy address
normalization); same e-mail domain (with accounting-office detection); shared
DDD+phone prefix; same nature+CNAE+capital-band; same registration-day+city+CNAE;
8-character CNPJ root clustering; **fuzzy partner-name matching** for small
spelling differences; and geographic proximity clustering after geocoding.

**5 · Investigative graph ergonomics:** edge labels naming the exact reason two
records link; filter by signal type and minimum cluster size; hide low-signal
attributes (CEP, capital, prefixes); isolate a ring on the map; **shortest path**
between two CNPJs; and **"why are these two connected?"**.

**6 · Workflow:** evidence **image attachments** (building/partner photos); a
**watchlist** with per-CNPJ recheck intervals; a **change timeline** and a
**diff of what changed** on each refresh; **CSV/JSON import & export** of
campaigns; and **campaign merge**.

**7 · Security / ops:** Leaflet, vis-network and the JetBrains Mono font are
**vendored locally** (no CDN, works offline); **CSRF protection** on every POST;
**upload limits** (≤ 8 MB, png/jpg/jpeg only); and a **queue panel** with retry
controls.

**8 · Alphanumeric CNPJ (July 2026).** The new `AA.AAA.AAA/AAAA-DV` format —
first 12 positions alphanumeric `[0-9A-Z]`, last 2 numeric check digits — is
fully supported alongside legacy numeric CNPJs. The check digit uses the same
modulo-11 routine where each base character contributes `ord(c) − '0'`. Verified
against the official reference `12.ABC.345/01DE-35`.

---

## How it works

1. **Create a campaign** on the home page.
2. **Add CNPJs** — paste in any format (legacy `35.478.926/0001-71` or
   alphanumeric `12.ABC.345/01DE-35`; commas, spaces, newlines all work) or
   upload a `.txt`/`.csv`. Check digits are validated; invalid numbers are
   rejected before queueing.
3. A background worker fetches each new CNPJ (~4/sec, with retries). On every
   successful fetch the record is normalized, child tables rebuilt, and a history
   snapshot stored. The progress counter updates live.
4. **Explore the tabs:** correlation map, geographic map, companies (with
   evidence trails), rings, shared attributes, insights, and queue & data ops.
5. **Geocode for map** resolves addresses via Nominatim (~1/sec) to plot
   companies and enable geographic-proximity clustering.

---

## Risk scoring

The score is the capped sum of evidence weights. Bands: `< 30` low · `30–59`
medium · `60–79` high · `80+` critical. Pairwise signals (shared owner,
address, e-mail, phone, root, etc.) and per-company heuristics (recent
registration, NULA/INAPTA/SUSPENSA/BAIXADA status, nominal/high-new capital,
MEI over the legal ceiling, risky CNAE, extreme partner age band, ownership
flip, corporate name-twins, ring membership) each contribute weighted evidence.
Confidence is reported separately from weight so weak-but-explanatory links
(e.g. a shared accountant) don't masquerade as proof.

---

## Files

```
app.py            Flask app: routes, CSRF, uploads, queue, background workers
cnpj_utils.py     CNPJ validation/formatting — legacy + 2026 alphanumeric
enrich.py         shared normalization: phones, e-mails, addresses, fuzzy names, geo
api_client.py     opencnpj + Nominatim clients; payload normalization
database.py       SQLite schema, migrations, normalized tables, history, watchlist
correlations.py   evidence-first engine: dimensions, rings, graph, path-finding
templates/        base, index, campaign, cnpj, search, partners, watchlist, queue
static/           style.css, campaign.js
static/vendor/    leaflet, vis-network, JetBrains Mono (vendored, offline)
uploads/          evidence images (created at runtime)
```

---

## Routes (selected)

| Route | Purpose |
|------|---------|
| `/campaigns/<id>/data` | full analysis JSON (companies, evidence, graph, clusters, rings) |
| `/campaigns/<id>/path?a=&b=` | shortest path between two CNPJs |
| `/campaigns/<id>/why?a=&b=` | direct links + shortest path between two CNPJs |
| `/campaigns/<id>/export.csv` · `.json` | export campaign |
| `/campaigns/import` · `/campaigns/merge` | import / merge campaigns |
| `/cnpj/<cnpj>/attachments` | upload evidence image (png/jpg/jpeg ≤ 8 MB) |
| `/cnpj/<cnpj>/watch` | add / update / remove watchlist entry |
| `/watchlist` · `/watchlist/recheck` | watchlist view + re-fetch due CNPJs |
| `/queue` · `/queue/status` · `/queue/requeue` | queue visibility + retry controls |

All state-changing requests require a CSRF token (form field `csrf_token` or
`X-CSRF-Token` header); the bundled JS sends it automatically.

---

## Notes & limitations

- The opencnpj API may rate-limit or block datacenter IPs; the client uses a
  browser User-Agent and retries. Run it from your own machine for best results.
- Map **tiles** (CartoDB) and **geocoding** (Nominatim) are online data sources,
  like the company API itself — only the front-end libraries are vendored.
- Alphanumeric CNPJs are accepted now so the platform is ready ahead of the
  July 2026 rollout; the public API may not return them until issuance begins.
- This is an analysis aid. Every signal is a lead to verify, not a verdict.
