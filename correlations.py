"""Correlation & fraud-signal engine (evidence-first).

Pure functions over the fetched CNPJ rows of a campaign. For every company the
engine emits an **evidence trail**: a list of records, each carrying the signal,
its weight, a confidence, the matched entities, the data source and a timestamp.
The 0-100 risk score is just the (capped) sum of evidence weights, so every
point is explainable and traceable. Risk flags are leads, not proof.

It also builds the bipartite correlation graph (with labelled edges), detects
rings (connected components), and exposes shortest-path / "why are these two
connected" helpers for investigative work.
"""

import re
import json
from collections import defaultdict, Counter
from datetime import date, datetime, timezone

import enrich

# --------------------------------------------------------------- weights -----
RISK_WEIGHTS = {
    "recent": 22, "very_recent": 13,
    "shared_address": 18, "address_farm": 9,
    "street_num_cep": 16, "shared_cep": 4,
    "shared_email": 16, "shared_email_domain": 9, "contact_hub": 7,
    "shared_phone": 13, "shared_phone_prefix": 6,
    "shared_partner": 20, "partner_fuzzy": 10, "serial_partner": 16,
    "shared_accountant": 9,
    "same_reg_day": 12, "regday_muni_cnae": 12,
    "juridical_cnae_capital": 7,
    "adjacent_base": 14, "same_root8": 12,
    "geo_cluster": 8,
    "same_capital": 5,
    "status_nula": 30, "status_inapta": 22, "status_suspensa": 18,
    "status_baixada": 12, "status_other": 10,
    "nominal_capital": 8, "high_new_capital": 12, "mei_over_limit": 10,
    "risky_cnae": 8, "extreme_age": 12, "ownership_flip": 14,
    "name_twin": 10, "ring_member": 6,
}

# pairwise dimensions that link two *different* companies together. Each entry:
#   weight, confidence, source, low_signal, multi (value list vs single)
PAIR_DIMS = {
    "partner":        dict(w="shared_partner",   conf=0.90, src="registry QSA",  low=False, label="Shared partner / owner"),
    "partner_fp":     dict(w="partner_fuzzy",    conf=0.55, src="derived (name)", low=False, label="Partner name near-match"),
    "address_full":   dict(w="shared_address",   conf=0.80, src="registry",      low=False, label="Same full address"),
    "street_num_cep": dict(w="street_num_cep",   conf=0.85, src="derived (addr)",low=False, label="Same street+number+CEP"),
    "cep":            dict(w="shared_cep",        conf=0.30, src="registry",      low=True,  label="Same CEP"),
    "email":          dict(w="shared_email",     conf=0.80, src="registry",      low=False, label="Same e-mail"),
    "email_domain":   dict(w="shared_email_domain", conf=0.60, src="derived",    low=False, label="Same e-mail domain"),
    "accountant":     dict(w="shared_accountant",conf=0.45, src="derived",       low=False, label="Same accounting contact"),
    "phone":          dict(w="shared_phone",     conf=0.75, src="registry",      low=False, label="Same phone"),
    "phone_prefix":   dict(w="shared_phone_prefix", conf=0.40, src="derived",    low=True,  label="Same DDD+phone prefix"),
    "regday_muni_cnae": dict(w="regday_muni_cnae", conf=0.60, src="derived",     low=False, label="Same open-date + city + activity"),
    "juridical_cnae_capital": dict(w="juridical_cnae_capital", conf=0.30, src="derived", low=True, label="Same nature + activity + capital band"),
    "root8":          dict(w="same_root8",       conf=0.70, src="registry",      low=False, label="Same 8-char CNPJ root"),
    "capital":        dict(w="same_capital",     conf=0.20, src="registry",      low=True,  label="Identical declared capital"),
    "regday":         dict(w="same_reg_day",     conf=0.45, src="registry",      low=False, label="Same registration day"),
    "geo":            dict(w="geo_cluster",      conf=0.50, src="geocode",       low=False, label="Geographically co-located"),
}

RISKY_CNAE_PREFIXES = {
    "6462": "Holdings of non-financial institutions",
    "6463": "Other equity holdings", "4690": "Non-specialized wholesale",
    "4789": "Non-specialized retail", "4713": "General-merchandise retail",
    "4731": "Wholesale/retail of fuels", "4681": "Wholesale of fuels",
    "8211": "Combined office services", "8299": "Other business support",
    "7020": "Generic management consulting", "4618": "Commercial agents of varied goods",
}
MEI_CAPITAL_CEILING = 81000.0
GEO_RADIUS_M = 150.0       # companies within this distance form a geo cluster

LEGAL_SUFFIXES = re.compile(
    r"\b(LTDA|EIRELI|EPP|ME|MEI|S\.?A\.?|SA|SS|EI|SOCIEDADE|EMPRESARIA|INDIVIDUAL|"
    r"COMERCIO|SERVICOS?|PARTICIPACOES|HOLDING)\b", re.I)


# --------------------------------------------------------------- helpers ------

def _today():
    return date.today()


def _parse_date(s):
    if not s or s in ("0000-00-00", ""):
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _months_old(d):
    if not d:
        return None
    t = _today()
    return (t.year - d.year) * 12 + (t.month - d.month) - (1 if t.day < d.day else 0)


def _phones(r):
    out = []
    try:
        for t in json.loads(r.get("telefones") or "[]"):
            out.append(enrich.phone_parts(t.get("ddd"), t.get("numero")))
    except (ValueError, TypeError):
        pass
    return out


def _qsa(r):
    try:
        return json.loads(r.get("qsa") or "[]")
    except (ValueError, TypeError):
        return []


def _partners(r):
    out = []
    for p in _qsa(r):
        name = (p.get("nome_socio") or "").strip()
        if not name:
            continue
        cpf = (p.get("cnpj_cpf_socio") or "").strip()
        up = name.upper()
        key = f"{cpf}::{up}" if cpf and cpf != "***000000**" else up
        out.append({"key": key, "label": name, "fp": enrich.name_fingerprint(name),
                    "faixa": (p.get("faixa_etaria") or "").strip(),
                    "entrada": _parse_date(p.get("data_entrada_sociedade") or p.get("data_entrada"))})
    return out


def _cnae_list(r):
    codes = set()
    pc = enrich.cnae_clean(r.get("cnae_principal"))
    if pc:
        codes.add(pc)
    try:
        for c in (json.loads(r.get("raw_json") or "{}").get("cnaes") or []):
            cc = enrich.cnae_clean(c.get("codigo") or c.get("cnae"))
            if cc:
                codes.add(cc)
    except (ValueError, TypeError):
        pass
    return codes


def _address_label(r):
    line = " ".join(x for x in [r.get("tipo_logradouro"), r.get("logradouro")] if x)
    if r.get("numero"):
        line += f", {r['numero']}"
    tail = " ".join(x for x in [r.get("bairro"), r.get("municipio"), r.get("uf")] if x)
    return f"{line} - {tail}".strip(" -")


def _name_core(razao):
    core = enrich.strip_accents_upper(razao)
    core = LEGAL_SUFFIXES.sub(" ", core)
    core = re.sub(r"[^A-Z0-9 ]", " ", core)
    return re.sub(r"\s+", " ", core).strip()


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


# ----------------------------------------------------- dimension indexing -----

def _build_index(rows):
    """Return {dim: {value: [cnpjs]}} plus human labels for values."""
    idx = {d: defaultdict(list) for d in PAIR_DIMS}
    labels = {d: {} for d in PAIR_DIMS}

    def add(dim, value, cnpj, label=None):
        if value in (None, "", "|||"):
            return
        idx[dim][value].append(cnpj)
        if label and value not in labels[dim]:
            labels[dim][value] = label

    fp_occ = defaultdict(list)
    fp_label = {}

    for r in rows:
        c = r["cnpj"]
        ak = enrich.address_keys(r)
        add("address_full", ak["full"], c, _address_label(r))
        add("street_num_cep", ak["street_num_cep"], c, _address_label(r))
        add("cep", ak["cep"], c, ak["cep"])
        em = (r.get("email") or "").strip().lower()
        if em:
            add("email", em, c, em)
            dom = enrich.email_domain(em)
            if dom and not enrich.is_freemail(dom):
                add("email_domain", dom, c, dom)
            if enrich.is_accounting(em):
                add("accountant", dom or em, c, dom or em)
        for ph in _phones(r):
            if ph["full"]:
                add("phone", ph["full"], c, ph["full"])
            if ph["prefix"] and len(ph["prefix"]) >= 5:
                add("phone_prefix", ph["prefix"], c, ph["prefix"])
        for p in _partners(r):
            add("partner", p["key"], c, p["label"])
            if p["fp"]:
                fp_occ[p["fp"]].append(c)
                fp_label.setdefault(p["fp"], p["label"])
        cap = r.get("capital_social")
        if cap is not None:
            add("capital", cap, c, f"R$ {cap}")
        d = _parse_date(r.get("data_inicio_atividade"))
        muni = enrich.strip_accents_upper(r.get("municipio"))
        cnaes = _cnae_list(r)
        cdiv = enrich.cnae_division(r.get("cnae_principal"))
        if d:
            add("regday", d.isoformat(), c, d.isoformat())
            if muni and cdiv:
                add("regday_muni_cnae", f"{d.isoformat()}|{muni}|{cdiv}", c,
                    f"{d.isoformat()} · {muni} · CNAE {cdiv}")
        nat = enrich.strip_accents_upper(r.get("natureza_juridica"))
        band = r.get("capital_band") or enrich.capital_band(cap)
        if nat and cdiv and band:
            add("juridical_cnae_capital", f"{nat}|{cdiv}|{band}", c,
                f"{nat} · CNAE {cdiv} · {band}")
        from cnpj_utils import root8
        rt = root8(c)
        if rt:
            add("root8", rt, c, rt)

    # partner_fp: fuzzy-cluster partner-name fingerprints so small spelling
    # differences (typos, doubled letters) collapse to one group.
    fps = list(fp_occ.keys())
    fuf = _UF()
    for fp in fps:
        fuf.find(fp)
    for i in range(len(fps)):
        for j in range(i + 1, len(fps)):
            if enrich.names_similar(fps[i], fps[j]):
                fuf.union(fps[i], fps[j])
    fp_groups = defaultdict(list)
    for fp in fps:
        fp_groups[fuf.find(fp)].append(fp)
    for root, members in fp_groups.items():
        # genuine near-match only: >=2 distinct spellings merged together.
        # exact same-spelling sharing is already covered by the `partner` dim.
        if len(members) < 2:
            continue
        cnpjs = sorted({c for fp in members for c in fp_occ[fp]})
        if len(cnpjs) < 2:
            continue
        rep = fp_label.get(root) or fp_label.get(members[0]) or root
        for c in cnpjs:
            idx["partner_fp"][root].append(c)
        labels["partner_fp"][root] = f"{rep} (~{len(members)} spellings)"

    # geo clusters (within GEO_RADIUS_M of each other)
    pts = [(r["cnpj"], r["lat"], r["lon"]) for r in rows if r.get("lat") and r.get("lon")]
    guf = _UF()
    for cnpj, _, _ in pts:
        guf.find(cnpj)
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            if enrich.haversine_km(pts[i][1], pts[i][2], pts[j][1], pts[j][2]) * 1000 <= GEO_RADIUS_M:
                guf.union(pts[i][0], pts[j][0])
    geo_groups = defaultdict(list)
    for cnpj, _, _ in pts:
        geo_groups[guf.find(cnpj)].append(cnpj)
    for root, members in geo_groups.items():
        if len(members) >= 2:
            for m in members:
                idx["geo"][f"geo_{root}"].append(m)
                labels["geo"][f"geo_{root}"] = f"{len(members)} companies < {int(GEO_RADIUS_M)}m apart"

    return idx, labels


def _clusters(idx, dim, labels):
    out = []
    for val, members in idx[dim].items():
        uniq = sorted(set(members))
        if len(uniq) >= 2:
            out.append({"value": labels[dim].get(val, val), "key": val,
                        "count": len(uniq), "cnpjs": uniq})
    return sorted(out, key=lambda x: -x["count"])


# ---------------------------------------------------------- main analysis -----

def build_analysis(rows, global_partner_counts=None):
    rows = [r for r in rows if r.get("fetch_status") == "ok"]
    global_partner_counts = global_partner_counts or {}
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    idx, labels = _build_index(rows)

    cluster_sets = {dim: _clusters(idx, dim, labels) for dim in PAIR_DIMS}

    # quick lookup: for a (dim, cnpj) -> the cluster it is in (members + value)
    member_cluster = {dim: {} for dim in PAIR_DIMS}
    for dim in PAIR_DIMS:
        for cl in cluster_sets[dim]:
            for c in cl["cnpjs"]:
                member_cluster[dim][c] = cl

    # adjacent CNPJ roots (numeric only — alphanumeric roots are not sequential)
    numeric = sorted((int(r["cnpj"][:8]), r["cnpj"]) for r in rows if r["cnpj"][:8].isdigit())
    adjacent = defaultdict(set)
    for (n1, c1), (n2, c2) in zip(numeric, numeric[1:]):
        if 0 < (n2 - n1) <= 50:
            adjacent[c1].add(c2); adjacent[c2].add(c1)

    # partner -> distinct companies (campaign + platform-wide)
    partner_camp = {k: len(set(v)) for k, v in idx["partner"].items()}

    # name twins
    name_core_map = defaultdict(set)
    for r in rows:
        core = _name_core(r.get("razao_social"))
        if len(core) >= 5:
            name_core_map[core].add(r["cnpj"])
    name_twins = {c: core for core, cs in name_core_map.items() if len(cs) >= 2 for c in cs}

    # ----- evidence + score per company -----
    W = RISK_WEIGHTS
    evidence = defaultdict(list)

    def ev(c, signal, weight, conf, value, matched, source, detail=None):
        evidence[c].append({
            "signal": signal, "weight": weight, "confidence": round(conf, 2),
            "value": value, "matched": matched, "source": source,
            "detail": detail or "", "ts": ts,
        })

    # pairwise dimension evidence
    for dim, meta in PAIR_DIMS.items():
        w = W[meta["w"]]
        for cl in cluster_sets[dim]:
            for c in cl["cnpjs"]:
                others = [x for x in cl["cnpjs"] if x != c]
                ev(c, dim, w, meta["conf"], cl["value"], others, meta["src"],
                   detail=meta["label"])

    for r in rows:
        c = r["cnpj"]
        months = _months_old(_parse_date(r.get("data_inicio_atividade")))
        if months is not None and months < 12:
            ev(c, "recent", W["recent"], 0.7, f"{months} months", [], "registry",
               f"Registered {months} month(s) ago")
            if months < 3:
                ev(c, "very_recent", W["very_recent"], 0.7, f"{months} months", [],
                   "registry", "Less than 90 days old")

        # address farm / contact hub escalations
        ac = member_cluster["address_full"].get(c)
        if ac and ac["count"] >= 5:
            ev(c, "address_farm", W["address_farm"], 0.7, ac["value"],
               [x for x in ac["cnpjs"] if x != c], "derived", "Address farm / virtual office")
        ec = member_cluster["email"].get(c)
        if ec and ec["count"] >= 5:
            ev(c, "contact_hub", W["contact_hub"], 0.6, ec["value"],
               [x for x in ec["cnpjs"] if x != c], "derived", "E-mail shared by many companies")

        # serial partner / laranja
        hub, hub_label = 0, ""
        for p in _partners(r):
            cnt = max(partner_camp.get(p["key"], 0), global_partner_counts.get(p["key"], 0))
            if cnt > hub:
                hub, hub_label = cnt, p["label"]
        if hub >= 3:
            ev(c, "serial_partner", W["serial_partner"], 0.8, hub_label, [], "derived",
               f"Partner owns {hub} companies (front-man pattern)")

        # adjacency / root8
        if adjacent.get(c):
            ev(c, "adjacent_base", W["adjacent_base"], 0.5, r["cnpj"][:8],
               sorted(adjacent[c]), "registry", "Adjacent CNPJ root number")

        # status
        sl = enrich.strip_accents_upper(r.get("situacao_cadastral"))
        if sl and sl != "ATIVA":
            if "NULA" in sl:
                ev(c, "status_nula", W["status_nula"], 0.9, r.get("situacao_cadastral"), [], "registry", "Status: NULA (declared void)")
            elif "INAPTA" in sl:
                ev(c, "status_inapta", W["status_inapta"], 0.85, r.get("situacao_cadastral"), [], "registry", "Status: INAPTA (stopped filing)")
            elif "SUSPENSA" in sl:
                ev(c, "status_suspensa", W["status_suspensa"], 0.8, r.get("situacao_cadastral"), [], "registry", "Status: SUSPENSA")
            elif "BAIXADA" in sl:
                ev(c, "status_baixada", W["status_baixada"], 0.7, r.get("situacao_cadastral"), [], "registry", "Status: BAIXADA (closed)")
            else:
                ev(c, "status_other", W["status_other"], 0.6, r.get("situacao_cadastral"), [], "registry", f"Status: {r.get('situacao_cadastral')}")

        # capital heuristics
        cap = r.get("capital_social")
        try:
            cap = float(cap) if cap is not None else None
        except (TypeError, ValueError):
            cap = None
        if cap is not None:
            if 0 <= cap <= 100:
                ev(c, "nominal_capital", W["nominal_capital"], 0.5, cap, [], "registry", "Nominal capital (<= R$100)")
            if cap >= 1_000_000 and months is not None and months < 12:
                ev(c, "high_new_capital", W["high_new_capital"], 0.6, cap, [], "registry", "High capital on a new company")
        if enrich.strip_accents_upper(r.get("opcao_mei")) in ("SIM", "S", "TRUE", "1") and cap and cap > MEI_CAPITAL_CEILING:
            ev(c, "mei_over_limit", W["mei_over_limit"], 0.6, cap, [], "registry", "MEI capital above legal ceiling")

        # risky CNAE
        pref = enrich.cnae_clean(r.get("cnae_principal"))[:4]
        if pref in RISKY_CNAE_PREFIXES:
            ev(c, "risky_cnae", W["risky_cnae"], 0.4, pref, [], "registry",
               f"High-risk activity: {RISKY_CNAE_PREFIXES[pref]}")

        # partner age + ownership flip
        plist = _partners(r)
        ages = []
        for p in plist:
            m = re.search(r"\d+", p["faixa"] or "")
            if m:
                ages.append(int(m.group()))
        if any(a >= 81 or a < 21 for a in ages):
            ev(c, "extreme_age", W["extreme_age"], 0.6, "81+/<21", [], "registry", "Partner age band is extreme (81+/<21)")
        entries = [p["entrada"] for p in plist if p["entrada"]]
        if plist and entries and len(entries) == len(plist) and months is not None and months >= 24:
            if all((_months_old(e) or 999) < 12 for e in entries):
                ev(c, "ownership_flip", W["ownership_flip"], 0.7, None, [], "derived", "All partners entered < 1y ago (older company)")

        if c in name_twins:
            ev(c, "name_twin", W["name_twin"], 0.5, name_twins[c], [], "derived", "Corporate name twins another company")

    # ----- company-company adjacency & rings (non-low-signal dims) -----
    adjacency = defaultdict(lambda: defaultdict(list))   # c -> neighbor -> [reasons]
    uf = _UF()
    for r in rows:
        uf.find(r["cnpj"])
    for dim, meta in PAIR_DIMS.items():
        for cl in cluster_sets[dim]:
            members = cl["cnpjs"]
            for a_i in range(len(members)):
                for b_i in range(a_i + 1, len(members)):
                    a, b = members[a_i], members[b_i]
                    adjacency[a][b].append({"dim": dim, "label": meta["label"], "value": cl["value"]})
                    adjacency[b][a].append({"dim": dim, "label": meta["label"], "value": cl["value"]})
            if not meta["low"]:
                for m in members[1:]:
                    uf.union(members[0], m)
    for c1, others in adjacent.items():
        for c2 in others:
            uf.union(c1, c2)
            adjacency[c1][c2].append({"dim": "adjacent", "label": "Adjacent CNPJ root", "value": c1[:8]})
            adjacency[c2][c1].append({"dim": "adjacent", "label": "Adjacent CNPJ root", "value": c1[:8]})

    comp_members = defaultdict(list)
    for r in rows:
        comp_members[uf.find(r["cnpj"])].append(r["cnpj"])
    component_of = {c: root for root, members in comp_members.items() for c in members}

    # score = capped sum of evidence weights (+ ring bonus)
    risk = {}
    for r in rows:
        c = r["cnpj"]
        score = sum(e["weight"] for e in evidence[c])
        if len(comp_members[component_of[c]]) >= 4:
            ev(c, "ring_member", W["ring_member"], 0.5, None, [], "derived",
               f"Part of a {len(comp_members[component_of[c]])}-company ring")
            score += W["ring_member"]
        risk[c] = min(score, 100)

    # ----- rings list -----
    rings = []
    for root, members in comp_members.items():
        if len(members) < 3:
            continue
        dims = set()
        for m in members:
            for nb, reasons in adjacency[m].items():
                if nb in members:
                    dims |= {x["dim"] for x in reasons}
        members_sorted = sorted(members, key=lambda m: -risk[m])
        rings.append({"id": str(root), "size": len(members), "cnpjs": members_sorted,
                      "dims": sorted(dims), "avg_risk": round(sum(risk[m] for m in members) / len(members)),
                      "max_risk": max(risk[m] for m in members)})
    rings.sort(key=lambda x: (-x["size"], -x["avg_risk"]))

    # ----- bipartite graph with labelled edges -----
    nodes, edges, connected = [], [], set()
    graph_dims = ["partner", "partner_fp", "address_full", "street_num_cep", "email",
                  "email_domain", "phone", "phone_prefix", "regday_muni_cnae",
                  "root8", "geo", "capital", "cep"]
    aid = 0
    for dim in graph_dims:
        meta = PAIR_DIMS[dim]
        for cl in cluster_sets[dim]:
            aid += 1
            attr_id = f"attr_{dim}_{aid}"
            lab = str(cl["value"])
            nodes.append({"id": attr_id, "type": "attr", "dim": dim,
                          "label": (lab[:42] + "…") if len(lab) > 43 else lab,
                          "full": lab, "count": cl["count"], "low": meta["low"]})
            for c in cl["cnpjs"]:
                edges.append({"source": attr_id, "target": c, "dim": dim,
                              "label": meta["label"], "low": meta["low"]})
                connected.add(c)

    for r in rows:
        c = r["cnpj"]
        nodes.append({"id": c, "type": "cnpj",
                      "label": (r.get("nome_fantasia") or r.get("razao_social") or c)[:28],
                      "razao": r.get("razao_social", ""), "risk": risk[c],
                      "ring": len(comp_members[component_of[c]]) >= 3,
                      "isolated": c not in connected})

    map_points = [
        {"cnpj": r["cnpj"], "lat": r["lat"], "lon": r["lon"],
         "name": r.get("razao_social") or r["cnpj"], "address": _address_label(r),
         "risk": risk[r["cnpj"]],
         "recent": (_months_old(_parse_date(r.get("data_inicio_atividade"))) or 99) < 12}
        for r in rows if r.get("lat") and r.get("lon")
    ]

    # ----- insights -----
    uf_dist = Counter(r.get("uf") for r in rows if r.get("uf"))
    cnae_dist = Counter(f"{r.get('cnae_principal','')} {r.get('cnae_principal_desc','')}".strip()
                        for r in rows if r.get("cnae_principal"))
    status_dist = Counter(r.get("situacao_cadastral") for r in rows if r.get("situacao_cadastral"))
    year_dist = Counter((_parse_date(r.get("data_inicio_atividade")) or date(1900, 1, 1)).year
                        for r in rows if _parse_date(r.get("data_inicio_atividade")))
    recent_list = sorted(
        ({"cnpj": r["cnpj"], "name": r.get("razao_social"), "date": r.get("data_inicio_atividade"),
          "months": _months_old(_parse_date(r.get("data_inicio_atividade")))}
         for r in rows if (_months_old(_parse_date(r.get("data_inicio_atividade"))) or 99) < 12),
        key=lambda x: x["date"] or "", reverse=True)

    def band(s):
        return "critical" if s >= 80 else "high" if s >= 60 else "medium" if s >= 30 else "low"

    companies = []
    for r in rows:
        c = r["cnpj"]
        evs = sorted(evidence[c], key=lambda e: -e["weight"])
        companies.append({
            "cnpj": c, "cnpj_fmt": _fmt(c), "razao_social": r.get("razao_social"),
            "nome_fantasia": r.get("nome_fantasia"), "uf": r.get("uf"),
            "municipio": r.get("municipio"), "situacao": r.get("situacao_cadastral"),
            "data_inicio": r.get("data_inicio_atividade"), "capital": r.get("capital_social"),
            "email": r.get("email"), "review_status": r.get("review_status") or "none",
            "ring_size": len(comp_members[component_of[c]]),
            "risk": risk[c], "band": band(risk[c]),
            "flags": [e["detail"] for e in evs if e["detail"]],
            "evidence": evs,
        })
    companies.sort(key=lambda x: -x["risk"])
    n_recent = sum(1 for r in companies if any("Registered" in f for f in r["flags"]))

    return {
        "summary": {
            "total": len(rows), "recent": n_recent,
            "shared_address_groups": len(cluster_sets["address_full"]),
            "shared_partner_groups": len(cluster_sets["partner"]),
            "shared_contact_groups": len(cluster_sets["email"]) + len(cluster_sets["phone"]),
            "rings": len(rings),
            "high_risk": sum(1 for r in companies if r["risk"] >= 60),
            "mapped": len(map_points),
        },
        "clusters": {k: cluster_sets[k] for k in (
            "partner", "partner_fp", "address_full", "street_num_cep", "cep", "email",
            "email_domain", "accountant", "phone", "phone_prefix", "regday",
            "regday_muni_cnae", "juridical_cnae_capital", "root8", "geo", "capital")},
        "rings": rings,
        "graph": {"nodes": nodes, "edges": edges},
        "map_points": map_points,
        "companies": companies,
        "insights": {"uf": uf_dist.most_common(), "cnae": cnae_dist.most_common(10),
                     "status": status_dist.most_common(), "year": sorted(year_dist.items()),
                     "recent_list": recent_list},
    }


def _fmt(c):
    try:
        from cnpj_utils import format_cnpj
        return format_cnpj(c)
    except Exception:
        return c


# --------------------------------------------- shortest path / why-connected --

def _adjacency(rows):
    idx, labels = _build_index(rows)
    adj = defaultdict(lambda: defaultdict(list))
    for dim, meta in PAIR_DIMS.items():
        for cl in _clusters(idx, dim, labels):
            members = cl["cnpjs"]
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = members[i], members[j]
                    link = {"dim": dim, "label": meta["label"], "value": cl["value"], "low": meta["low"]}
                    adj[a][b].append(link); adj[b][a].append(link)
    return adj


def shortest_path(rows, a, b):
    """BFS shortest path between two CNPJs across shared-attribute links.
    Returns {found, path:[cnpj...], links:[[reason...]...]} ."""
    rows = [r for r in rows if r.get("fetch_status") == "ok"]
    a, b = a.strip(), b.strip()
    present = {r["cnpj"] for r in rows}
    if a not in present or b not in present:
        return {"found": False, "reason": "one or both CNPJs are not in this campaign"}
    if a == b:
        return {"found": True, "path": [a], "links": []}
    adj = _adjacency(rows)
    from collections import deque
    prev = {a: None}
    q = deque([a])
    while q:
        cur = q.popleft()
        if cur == b:
            break
        for nb in adj[cur]:
            if nb not in prev:
                prev[nb] = cur
                q.append(nb)
    if b not in prev:
        return {"found": False, "reason": "no connection path found"}
    path = []
    node = b
    while node is not None:
        path.append(node)
        node = prev[node]
    path.reverse()
    links = [adj[path[i]][path[i + 1]] for i in range(len(path) - 1)]
    return {"found": True, "path": path, "path_fmt": [_fmt(x) for x in path], "links": links}


def why_connected(rows, a, b):
    """Direct shared attributes between two companies (if any) plus the shortest
    path. Answers 'why are these two connected?'."""
    rows = [r for r in rows if r.get("fetch_status") == "ok"]
    adj = _adjacency(rows)
    direct = adj.get(a, {}).get(b, [])
    return {"direct": direct, "path": shortest_path(rows, a, b)}
