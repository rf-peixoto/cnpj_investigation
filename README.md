# cnpjscope — CNPJ correlation & fraud-analysis platform

A self-hosted Flask app for investigating clusters of Brazilian companies.
You group CNPJs into **campaigns**, the app pulls each record from the free
[opencnpj.org](https://opencnpj.org) API, then surfaces every correlation it can
find — shared owners, addresses, phones, e-mails, accountants, identical
capital, same registration day, numerically-adjacent registration numbers — and
scores each company for fraud risk. It also plots companies on a map and lets
you search the whole pool across campaigns.

Visual style: terminal / TUI — white-on-black, colour only on signal.

---

## Quick start

```bash
cd cnpj_platform
python3 -m venv .venv && source .venv/bin/activate      # optional but recommended
pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000>.

A SQLite file `cnpj_platform.db` is created next to the code (override the path
with the `CNPJ_DB` environment variable). Fetched records are cached there, so
the same CNPJ is never pulled twice across campaigns.

---

## How it works

1. **Create a campaign** on the home page.
2. **Add CNPJs** — paste them in any format (`35.478.926/0001-71`, commas,
   spaces, newlines all work) or upload a `.txt`/`.csv`. Punctuation is stripped
   and duplicates are ignored.
3. A background worker fetches each new CNPJ from the API (~4/sec, with retries).
   The progress counter updates live; the views populate as data arrives.
4. **Explore the tabs:**
   - **Correlation map** — a network graph. Round nodes are companies (ring
     colour = risk); boxes are the *shared things* (an owner, an address, a
     phone…). An edge means "this company has this attribute". Double-click a
     company to open its record.
   - **Geographic map** — click **geocode for map** once to resolve addresses to
     coordinates via OpenStreetMap/Nominatim (rate-limited to ~1/sec, cached).
     Red markers = high risk, amber = recently created.
   - **Companies** — every company sorted by risk, with the flags that produced
     the score, a ⬡ badge when it belongs to a ring, and any investigator status.
     The ✕ removes a company from the campaign. **Export CSV** downloads the whole
     campaign (companies, risk scores, flags, partners, review status).
   - **Rings** — groups of 3+ companies tied together by one or more shared
     signals (owner / address / contact / sequential registration), ranked by
     size and average risk. These are the suspected networks to investigate first.
   - **Shared attributes** — the raw correlation groups for each dimension.
   - **Insights** — recently-created list, distribution by state, CNAE activity,
     registration year, and status.
5. **Global search** (top nav) queries every fetched company across all
   campaigns, with filters for partner, e-mail, CNAE, city, district, UF, status,
   and an "under 1 year old" toggle.
6. **Serial partners** (top nav) lists owners attached to several companies
   across the whole platform — the classic front-man (*laranja*) pattern.
7. **Investigator review** — on any company's page, set a status (suspicious /
   cleared / confirmed) and a free-text note. These persist and show up in the
   companies table and CSV export.

Add or remove CNPJs from a campaign at any time; correlations recompute on the
next data load.

## Risk signals

Each company's 0–100 score is the sum of the signals it trips (score is capped
at 100). Bands: **<30** low · **30–59** medium · **60–79** high · **80+** critical.

| signal | weight | meaning |
|---|---|---|
| status: NULA | 30 | registration declared void — fraud by declaration |
| recently created | 22 (+13 if < 90 days) | registered < 1 year ago |
| status: INAPTA | 22 | stopped filing — classic "notas frias" front |
| shared partner | 20 | a partner/owner appears in another company here |
| status: SUSPENSA | 18 | registration suspended |
| shared address | 18 (+9 if 5+ companies) | same address; extra = address farm / virtual office |
| serial partner (*laranja*) | 16 | a partner of this company owns 3+ companies (campaign or platform-wide) |
| shared e-mail | 16 (+7 if 5+ companies) | identical contact e-mail; extra = contact hub |
| adjacent CNPJ root | 14 | registration number within 50 of another (batch creation) |
| ownership flip | 14 | older company whose partners all entered < 1 year ago |
| shared phone | 13 (+7 if 5+ companies) | identical phone; extra = contact hub |
| status: BAIXADA / other | 12 / 10 | closed, or any other non-*Ativa* status |
| high capital, new company | 12 | ≥ R$1M capital on a company < 1 year old |
| extreme partner age | 12 | a partner in the 81+ or under-21 band |
| same registration day | 12 | opened the exact same date as others |
| MEI over ceiling | 10 | flagged as MEI but capital above the legal limit |
| name twin | 10 | corporate name normalizes to the same core as another company |
| shared accountant | 9 | contact e-mail looks like a shared bookkeeper |
| nominal capital | 8 | declared capital ≤ R$100 |
| high-risk CNAE | 8 | activity disproportionately used in shells (holdings, non-specialized wholesale, fuel, generic office services…) |
| ring member | 6 | belongs to a correlation ring of 4+ companies |
| identical capital | 5 | same declared capital social as others |

**Rings** are connected components of the correlation graph: companies are joined
when they share an owner, address, phone, e-mail, accountant, registration day,
or sit on adjacent registration numbers. Any component of 3+ companies surfaces
on the Rings tab.

Partners are matched on the masked CPF **plus** name, so the same individual is
linked across companies even though the API only exposes the middle CPF digits.
The serial-partner signal also counts a partner's companies platform-wide, so a
front-man spread across several campaigns still lights up.

## Notes & limits

- **opencnpj** is free and unofficial; if you ever get HTTP 403, it's usually a
  Cloudflare bot-challenge on your IP — try again, or run from a residential
  connection. A browser-like User-Agent is already sent.
- **Nominatim** usage policy allows ~1 request/sec and forbids heavy bulk use.
  Geocoding here respects that and caches results, but for thousands of
  addresses use a dedicated geocoder (e.g. a self-hosted Nominatim or a paid
  provider) and swap the function in `api_client.geocode_address`.
- This is a single-user local analysis tool (Flask dev server, no auth). Don't
  expose it to the open internet as-is.
- CPF/CNPJ data is partially masked by the source API; treat all output as
  investigative leads, not proof.

## Files

```
app.py            Flask routes + background fetch/geocode workers
database.py       SQLite schema and all queries
api_client.py     opencnpj fetch, payload normalization, Nominatim geocoding
correlations.py   correlation clustering, risk scoring, graph/map/insight builders
templates/        base, index, campaign, cnpj, search, partners
static/           style.css (terminal theme), campaign.js (graph/map/tables/rings)
```
