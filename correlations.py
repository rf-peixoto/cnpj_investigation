"""Correlation & fraud-signal engine.

Pure functions over the list of successfully-fetched CNPJ rows in a campaign.
Builds shared-attribute clusters, a bipartite correlation graph, ring
(connected-component) detection, per-company risk scores with human-readable
flags, map points, and aggregate insight tables. Nothing here touches the
network or the database, so it is trivial to test.
"""

import json
import re
import unicodedata
from collections import defaultdict, Counter
from datetime import date, datetime

# ---------------------------------------------------------------- weights -----
# Points each signal contributes to a company's 0-100 risk score.
RISK_WEIGHTS = {
    "recent": 22,             # registered < 1 year ago
    "very_recent": 13,        # extra if < 90 days (stacks with recent)
    "shared_address": 18,
    "address_farm": 9,        # extra: 5+ companies at one address (virtual office)
    "shared_email": 16,
    "shared_phone": 13,
    "contact_hub": 7,         # extra: a phone/e-mail shared by many companies
    "shared_partner": 20,
    "serial_partner": 16,     # a partner of this co. owns many companies (laranja)
    "shared_accountant": 9,
    "same_reg_day": 12,
    "adjacent_base": 14,
    "same_capital": 5,
    # status-specific (mutually exclusive; only the matched one applies)
    "status_nula": 30,
    "status_inapta": 22,
    "status_suspensa": 18,
    "status_baixada": 12,
    "status_other": 10,
    # standalone heuristics
    "nominal_capital": 8,     # capital <= R$100
    "high_new_capital": 12,   # large capital on a < 1y company
    "mei_over_limit": 10,     # MEI/Simples declaring above legal ceiling
    "risky_cnae": 8,
    "extreme_age": 12,        # a partner 81+ or under 21
    "ownership_flip": 14,     # old company, all partners entered < 1y ago
    "name_twin": 10,
    "ring_member": 6,         # belongs to a >=4-company correlation ring
}

ATTR_LABELS = {
    "address": "Address", "email": "E-mail", "phone": "Phone",
    "partner": "Partner / owner", "capital": "Capital",
    "regday": "Registration day", "accountant": "Accountant",
}

# CNAE prefixes disproportionately seen in shell/fraud schemes. Soft signal only.
RISKY_CNAE_PREFIXES = {
    "6462": "Holdings of non-financial institutions",
    "6463": "Other equity holdings",
    "4690": "Non-specialized wholesale trade",
    "4789": "Non-specialized retail trade",
    "4713": "General-merchandise retail",
    "4731": "Wholesale/retail of fuels",
    "4681": "Wholesale of fuels & lubricants",
    "8211": "Combined office-administrative services",
    "8299": "Other business support services",
    "7020": "Business-management consulting (generic)",
    "4618": "Commercial agents of varied goods",
}

MEI_CAPITAL_CEILING = 81000.0   # MEI annual revenue ceiling (proxy sanity check)

LEGAL_SUFFIXES = re.compile(
    r"\b(LTDA|EIRELI|EPP|ME|MEI|S\.?A\.?|SA|SS|EI|SOCIEDADE|EMPRESARIA|"
    r"INDIVIDUAL|DE\s+RESPONSABILIDADE\s+LIMITADA|COMERCIO|SERVICOS?|"
    r"PARTICIPACOES|HOLDING)\b", re.I)


# ---------------------------------------------------------------- helpers -----

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


def _ascii_upper(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return s.upper().strip()


def _address_key(r):
    parts = [r.get("logradouro") or "", r.get("numero") or "",
             r.get("bairro") or "", r.get("municipio") or "", r.get("uf") or ""]
    key = "|".join(p.strip().upper() for p in parts)
    return key if any(p.strip() for p in parts) else None


def _address_label(r):
    line = " ".join(x for x in [r.get("tipo_logradouro"), r.get("logradouro")] if x)
    if r.get("numero"):
        line += f", {r['numero']}"
    tail = " ".join(x for x in [r.get("bairro"), r.get("municipio"), r.get("uf")] if x)
    return f"{line} - {tail}".strip(" -")


def _phones(r):
    out = []
    try:
        for t in json.loads(r.get("telefones") or "[]"):
            num = f"{t.get('ddd','')}{t.get('numero','')}".strip()
            if num:
                out.append(num)
    except (ValueError, TypeError):
        pass
    return out


def _partners_full(r):
    """Return list of dicts: {key, label, faixa, entrada} for each QSA partner."""
    out = []
    try:
        for p in json.loads(r.get("qsa") or "[]"):
            name = (p.get("nome_socio") or "").strip().upper()
            cpf = (p.get("cnpj_cpf_socio") or "").strip()
            if not name:
                continue
            key = f"{cpf}::{name}" if cpf and cpf != "***000000**" else name
            out.append({
                "key": key,
                "label": p.get("nome_socio", ""),
                "faixa": (p.get("faixa_etaria") or "").strip(),
                "entrada": _parse_date(p.get("data_entrada_sociedade")
                                       or p.get("data_entrada")),
            })
    except (ValueError, TypeError):
        pass
    return out


def _accountant_email(email):
    if not email:
        return None
    low = email.lower()
    if any(h in low for h in ("contab", "assessor", "escritorio", "fiscal", "conta")):
        return low
    return None


def _faixa_low(faixa):
    """First integer in a faixa_etaria string ('81 a 90' -> 81)."""
    m = re.search(r"\d+", faixa or "")
    return int(m.group()) if m else None


def _name_core(razao):
    core = _ascii_upper(razao)
    core = LEGAL_SUFFIXES.sub(" ", core)
    core = re.sub(r"[^A-Z0-9 ]", " ", core)
    core = re.sub(r"\s+", " ", core).strip()
    return core


# ------------------------------------------------------------ union-find ------

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


# --------------------------------------------------------------- analysis -----

def build_analysis(rows, global_partner_counts=None):
    """rows: list of cnpj dict rows. global_partner_counts: optional
    {partner_key: company_count} computed across the whole platform so a
    front-man who is spread over many campaigns still lights up."""
    rows = [r for r in rows if r.get("fetch_status") == "ok"]
    global_partner_counts = global_partner_counts or {}

    idx = {k: defaultdict(list) for k in
           ("address", "email", "phone", "partner", "accountant", "capital", "regday")}
    address_labels, partner_labels = {}, {}
    partner_extra = {}      # cnpj -> list of partner dicts
    name_core_map = defaultdict(set)   # core -> {cnpj}

    for r in rows:
        c = r["cnpj"]
        ak = _address_key(r)
        if ak:
            idx["address"][ak].append(c)
            address_labels[ak] = _address_label(r)
        if r.get("email"):
            idx["email"][r["email"]].append(c)
            acc = _accountant_email(r["email"])
            if acc:
                idx["accountant"][acc].append(c)
        for ph in _phones(r):
            idx["phone"][ph].append(c)
        plist = _partners_full(r)
        partner_extra[c] = plist
        for p in plist:
            idx["partner"][p["key"]].append(c)
            partner_labels[p["key"]] = p["label"]
        if r.get("capital_social"):
            idx["capital"][r["capital_social"]].append(c)
        d = _parse_date(r.get("data_inicio_atividade"))
        if d:
            idx["regday"][d.isoformat()].append(c)
        core = _name_core(r.get("razao_social"))
        if len(core) >= 5:
            name_core_map[core].add(c)

    # numerically adjacent CNPJ roots (batch-registration signal)
    roots = sorted((int(r["cnpj"][:8]), r["cnpj"]) for r in rows)
    adjacent = defaultdict(set)
    for (n1, c1), (n2, c2) in zip(roots, roots[1:]):
        if 0 < (n2 - n1) <= 50:
            adjacent[c1].add(c2)
            adjacent[c2].add(c1)

    # partner -> distinct companies in THIS campaign
    partner_camp_count = {k: len(set(v)) for k, v in idx["partner"].items()}

    # name twins
    name_twins = {c: core for core, cs in name_core_map.items()
                  if len(cs) >= 2 for c in cs}

    def clusters(dim, label_map=None):
        out = []
        for val, members in idx[dim].items():
            uniq = sorted(set(members))
            if len(uniq) >= 2:
                out.append({"value": (label_map or {}).get(val, val), "key": val,
                            "count": len(uniq), "cnpjs": uniq})
        return sorted(out, key=lambda x: -x["count"])

    cluster_sets = {
        "address": clusters("address", address_labels),
        "email": clusters("email"),
        "phone": clusters("phone"),
        "partner": clusters("partner", partner_labels),
        "accountant": clusters("accountant"),
        "capital": clusters("capital"),
        "regday": clusters("regday"),
    }

    # quick membership lookups + cluster size per company per dim
    def size_for(dim, cnpj):
        best = 0
        for cl in cluster_sets[dim]:
            if cnpj in cl["cnpjs"]:
                best = max(best, cl["count"])
        return best

    # ---- connected-component ("ring") detection over companies ----
    uf = _UF()
    for r in rows:
        uf.find(r["cnpj"])
    link_dims = defaultdict(set)   # frozenset linking -> dims (for ring summary)
    for dim in ("address", "email", "phone", "partner", "accountant", "regday"):
        for cl in cluster_sets[dim]:
            members = cl["cnpjs"]
            for o in members[1:]:
                uf.union(members[0], o)
            for m in members:
                link_dims[m].add(dim)
    for c1, others in adjacent.items():
        for c2 in others:
            uf.union(c1, c2)
            link_dims[c1].add("adjacent"); link_dims[c2].add("adjacent")

    comp_members = defaultdict(list)
    for r in rows:
        comp_members[uf.find(r["cnpj"])].append(r["cnpj"])
    component_of = {c: root for root, members in comp_members.items() for c in members}

    # ---- risk scoring ----
    risk = {}
    for r in rows:
        c = r["cnpj"]
        score, flags = 0, []
        W = RISK_WEIGHTS

        months = _months_old(_parse_date(r.get("data_inicio_atividade")))
        if months is not None and months < 12:
            score += W["recent"]; flags.append(f"Registered {months} month(s) ago")
            if months < 3:
                score += W["very_recent"]; flags.append("Less than 90 days old")

        asz = size_for("address", c)
        if asz >= 2:
            score += W["shared_address"]; flags.append(f"Shares address ({asz} cos.)")
            if asz >= 5:
                score += W["address_farm"]; flags.append("Address farm / virtual office")
        esz = size_for("email", c)
        if esz >= 2:
            score += W["shared_email"]; flags.append(f"Shares e-mail ({esz} cos.)")
            if esz >= 5:
                score += W["contact_hub"]; flags.append("E-mail used by many companies")
        psz = size_for("phone", c)
        if psz >= 2:
            score += W["shared_phone"]; flags.append(f"Shares phone ({psz} cos.)")
            if psz >= 5 and "E-mail used by many companies" not in flags:
                score += W["contact_hub"]; flags.append("Phone used by many companies")

        if size_for("partner", c) >= 2:
            score += W["shared_partner"]; flags.append("Shares a partner")
        # serial partner / laranja: any partner across many companies
        hub = 0
        for p in partner_extra.get(c, []):
            cnt = max(partner_camp_count.get(p["key"], 0),
                      global_partner_counts.get(p["key"], 0))
            hub = max(hub, cnt)
        if hub >= 3:
            score += W["serial_partner"]
            flags.append(f"Partner owns {hub} companies (front-man pattern)")

        if size_for("accountant", c) >= 2:
            score += W["shared_accountant"]; flags.append("Shared accountant e-mail")
        if size_for("regday", c) >= 2:
            score += W["same_reg_day"]; flags.append("Same registration day as others")
        if size_for("capital", c) >= 2:
            score += W["same_capital"]; flags.append("Identical declared capital")
        if adjacent.get(c):
            score += W["adjacent_base"]; flags.append("Adjacent CNPJ root number")

        # status-specific
        sit = (r.get("situacao_cadastral") or "")
        sl = _ascii_upper(sit)
        if sl and sl != "ATIVA":
            if "NULA" in sl:
                score += W["status_nula"]; flags.append("Status: NULA (declared void)")
            elif "INAPTA" in sl:
                score += W["status_inapta"]; flags.append("Status: INAPTA (stopped filing)")
            elif "SUSPENSA" in sl:
                score += W["status_suspensa"]; flags.append("Status: SUSPENSA")
            elif "BAIXADA" in sl:
                score += W["status_baixada"]; flags.append("Status: BAIXADA (closed)")
            else:
                score += W["status_other"]; flags.append(f"Status: {sit}")

        # capital heuristics
        cap = r.get("capital_social")
        try:
            cap = float(cap) if cap is not None else None
        except (TypeError, ValueError):
            cap = None
        if cap is not None:
            if 0 <= cap <= 100:
                score += W["nominal_capital"]; flags.append("Nominal capital (<= R$100)")
            if cap >= 1_000_000 and (months is not None and months < 12):
                score += W["high_new_capital"]; flags.append("High capital on a new company")
        mei = _ascii_upper(r.get("opcao_mei"))
        if mei in ("SIM", "S", "TRUE", "1") and cap and cap > MEI_CAPITAL_CEILING:
            score += W["mei_over_limit"]; flags.append("MEI capital above legal ceiling")

        # risky CNAE
        cnae = re.sub(r"\D", "", r.get("cnae_principal") or "")[:4]
        if cnae in RISKY_CNAE_PREFIXES:
            score += W["risky_cnae"]
            flags.append(f"High-risk activity: {RISKY_CNAE_PREFIXES[cnae]}")

        # partner age + ownership flip
        plist = partner_extra.get(c, [])
        ages = [_faixa_low(p["faixa"]) for p in plist if _faixa_low(p["faixa"]) is not None]
        if any(a >= 81 or a < 21 for a in ages):
            score += W["extreme_age"]; flags.append("Partner age band is extreme (81+/<21)")
        entries = [p["entrada"] for p in plist if p["entrada"]]
        if plist and entries and len(entries) == len(plist):
            if months is not None and months >= 24 and all(
                    (_months_old(e) or 999) < 12 for e in entries):
                score += W["ownership_flip"]
                flags.append("All partners entered < 1y ago (older company)")

        if c in name_twins:
            score += W["name_twin"]; flags.append("Corporate name twins another company")

        comp_size = len(comp_members[component_of[c]])
        if comp_size >= 4:
            score += W["ring_member"]; flags.append(f"Part of a {comp_size}-company ring")

        risk[c] = {"score": min(score, 100), "flags": flags}

    # ---- rings list (components of >= 3 companies) ----
    rings = []
    for root, members in comp_members.items():
        if len(members) < 3:
            continue
        dims = set()
        for m in members:
            dims |= link_dims.get(m, set())
        total = sum(risk[m]["score"] for m in members)
        members_sorted = sorted(members, key=lambda m: -risk[m]["score"])
        rings.append({
            "id": root,
            "size": len(members),
            "cnpjs": members_sorted,
            "dims": sorted(dims),
            "avg_risk": round(total / len(members)),
            "max_risk": max(risk[m]["score"] for m in members),
        })
    rings.sort(key=lambda x: (-x["size"], -x["avg_risk"]))

    # ---- bipartite correlation graph ----
    nodes, edges, connected = [], [], set()
    attr_dims = [("address", address_labels), ("email", {}), ("phone", {}),
                 ("partner", partner_labels), ("capital", {})]
    aid = 0
    for dim, _ in attr_dims:
        for cl in cluster_sets[dim]:
            aid += 1
            attr_id = f"attr_{dim}_{aid}"
            label = str(cl["value"])
            if dim == "capital":
                try:
                    label = f"R$ {float(cl['value']):,.2f}"
                except (TypeError, ValueError):
                    pass
            nodes.append({"id": attr_id, "type": "attr", "dim": dim,
                          "label": (label[:42] + "…") if len(label) > 43 else label,
                          "full": label, "count": cl["count"]})
            for c in cl["cnpjs"]:
                edges.append({"source": attr_id, "target": c, "dim": dim})
                connected.add(c)

    for r in rows:
        c = r["cnpj"]
        nodes.append({"id": c, "type": "cnpj",
                      "label": (r.get("nome_fantasia") or r.get("razao_social") or c)[:28],
                      "razao": r.get("razao_social", ""),
                      "risk": risk[c]["score"],
                      "ring": len(comp_members[component_of[c]]) >= 3,
                      "isolated": c not in connected})

    # ---- map points ----
    map_points = [
        {"cnpj": r["cnpj"], "lat": r["lat"], "lon": r["lon"],
         "name": r.get("razao_social") or r["cnpj"], "address": _address_label(r),
         "risk": risk[r["cnpj"]]["score"],
         "recent": (_months_old(_parse_date(r.get("data_inicio_atividade"))) or 99) < 12}
        for r in rows if r.get("lat") and r.get("lon")
    ]

    # ---- aggregate insights ----
    uf_dist = Counter(r.get("uf") for r in rows if r.get("uf"))
    cnae_dist = Counter(
        f"{r.get('cnae_principal','')} {r.get('cnae_principal_desc','')}".strip()
        for r in rows if r.get("cnae_principal"))
    status_dist = Counter(r.get("situacao_cadastral") for r in rows if r.get("situacao_cadastral"))
    year_dist = Counter(
        (_parse_date(r.get("data_inicio_atividade")) or date(1900, 1, 1)).year
        for r in rows if _parse_date(r.get("data_inicio_atividade")))
    recent_list = sorted(
        ({"cnpj": r["cnpj"], "name": r.get("razao_social"),
          "date": r.get("data_inicio_atividade"),
          "months": _months_old(_parse_date(r.get("data_inicio_atividade")))}
         for r in rows
         if (_months_old(_parse_date(r.get("data_inicio_atividade"))) or 99) < 12),
        key=lambda x: x["date"] or "", reverse=True)

    def band(s):
        return ("critical" if s >= 80 else "high" if s >= 60
                else "medium" if s >= 30 else "low")

    company_rows = []
    for r in rows:
        c = r["cnpj"]
        company_rows.append({
            "cnpj": c, "razao_social": r.get("razao_social"),
            "nome_fantasia": r.get("nome_fantasia"),
            "uf": r.get("uf"), "municipio": r.get("municipio"),
            "situacao": r.get("situacao_cadastral"),
            "data_inicio": r.get("data_inicio_atividade"),
            "capital": r.get("capital_social"), "email": r.get("email"),
            "review_status": r.get("review_status") or "none",
            "ring_size": len(comp_members[component_of[c]]),
            "risk": risk[c]["score"], "band": band(risk[c]["score"]),
            "flags": risk[c]["flags"]})
    company_rows.sort(key=lambda x: -x["risk"])

    n_recent = sum(1 for r in company_rows if any("Registered" in f for f in r["flags"]))

    return {
        "summary": {
            "total": len(rows), "recent": n_recent,
            "shared_address_groups": len(cluster_sets["address"]),
            "shared_partner_groups": len(cluster_sets["partner"]),
            "shared_contact_groups": len(cluster_sets["email"]) + len(cluster_sets["phone"]),
            "rings": len(rings),
            "high_risk": sum(1 for r in company_rows if r["risk"] >= 60),
            "mapped": len(map_points),
        },
        "clusters": cluster_sets,
        "rings": rings,
        "graph": {"nodes": nodes, "edges": edges},
        "map_points": map_points,
        "companies": company_rows,
        "insights": {
            "uf": uf_dist.most_common(), "cnae": cnae_dist.most_common(10),
            "status": status_dist.most_common(), "year": sorted(year_dist.items()),
            "recent_list": recent_list,
        },
    }
