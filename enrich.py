"""Normalization & enrichment helpers shared by the API client and the
correlation engine. Pure functions, no I/O."""

import re
import math
import unicodedata

# free / disposable mailbox providers — a shared address on one of these is far
# weaker evidence than a shared private/corporate domain.
FREEMAIL = {
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.com.br",
    "bol.com.br", "uol.com.br", "terra.com.br", "ig.com.br", "live.com",
    "icloud.com", "msn.com", "globo.com", "r7.com", "zipmail.com.br",
}

# tokens that suggest an accounting / bookkeeping office in an e-mail or name
ACCOUNTING_HINTS = ("contab", "contabil", "assessor", "escritorio", "fiscal",
                    "conta", "tribut", "auditoria", "pericia", "consultoria")


def strip_accents_upper(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().upper()


# ----------------------------------------------------------------- e-mail ----

def email_domain(email):
    if not email or "@" not in email:
        return ""
    return email.split("@", 1)[1].strip().lower()


def is_freemail(domain):
    return domain.lower() in FREEMAIL if domain else False


def is_accounting(text):
    """True if an e-mail (local or domain part) or name looks like a bookkeeper."""
    if not text:
        return False
    low = strip_accents_upper(text).lower()
    return any(h in low for h in ACCOUNTING_HINTS)


def email_parts(email):
    email = (email or "").strip().lower()
    domain = email_domain(email)
    return {
        "email": email,
        "domain": domain,
        "is_free": is_freemail(domain),
        "is_accounting": is_accounting(email),
    }


# ------------------------------------------------------------------ phone ----

def phone_parts(ddd, numero):
    ddd = re.sub(r"\D", "", str(ddd or ""))
    num = re.sub(r"\D", "", str(numero or ""))
    full = f"{ddd}{num}"
    # prefix = DDD + first 4 local digits (catches sequential blocks issued
    # together, e.g. a call-centre or a single operator behind many companies)
    prefix = f"{ddd}{num[:4]}" if num else ddd
    return {"ddd": ddd, "numero": num, "full": full, "prefix": prefix}


# ------------------------------------------------------------------ cnae -----

def cnae_clean(code):
    return re.sub(r"\D", "", str(code or ""))


def cnae_division(code):
    """First 2 digits — the CNAE 'division' (broad sector)."""
    c = cnae_clean(code)
    return c[:2] if len(c) >= 2 else ""


# --------------------------------------------------------------- address -----

_LOGR_PREFIX = re.compile(
    r"^(RUA|R|AV|AVENIDA|TRAVESSA|TV|ALAMEDA|AL|PRACA|PCA|RODOVIA|ROD|"
    r"ESTRADA|EST|VIA|LARGO|BECO|VIELA|QUADRA|Q)\b\.?", re.I)


def normalize_logradouro(tipo, logradouro):
    """Fuzzy street normalization: drop the type prefix, accents, punctuation,
    so 'R. Sao Joao' and 'RUA SÃO JOÃO' collapse to the same token."""
    base = " ".join(x for x in [tipo, logradouro] if x)
    base = strip_accents_upper(base)
    base = _LOGR_PREFIX.sub("", base).strip()
    base = re.sub(r"[^A-Z0-9 ]", " ", base)
    return re.sub(r"\s+", " ", base).strip()


def cep_digits(cep):
    c = re.sub(r"\D", "", str(cep or ""))
    return c if len(c) == 8 else ""


def address_keys(row):
    """Return the several address correlation keys for a record."""
    logr = normalize_logradouro(row.get("tipo_logradouro"), row.get("logradouro"))
    num = re.sub(r"\s+", "", str(row.get("numero") or "")).upper()
    cep = cep_digits(row.get("cep"))
    muni = strip_accents_upper(row.get("municipio"))
    uf = strip_accents_upper(row.get("uf"))
    return {
        "full": "|".join([logr, num, muni, uf]) if logr else "",
        "street_num_cep": "|".join([logr, num, cep]) if logr and cep else "",
        "cep": cep,
        "logradouro_norm": logr,
    }


# -------------------------------------------------------------- capital ------

def capital_band(value):
    """Bucket capital social into bands so 'similar capital' can be correlated
    without requiring an exact match."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if v <= 0:
        return "0"
    for hi, label in [(1_000, "<=1k"), (10_000, "1k-10k"), (50_000, "10k-50k"),
                      (100_000, "50k-100k"), (500_000, "100k-500k"),
                      (1_000_000, "500k-1M"), (10_000_000, "1M-10M")]:
        if v <= hi:
            return label
    return ">10M"


# ----------------------------------------------------- fuzzy name matching ---

def name_tokens(name):
    txt = strip_accents_upper(name)
    txt = re.sub(r"[^A-Z0-9 ]", " ", txt)
    return [t for t in txt.split() if len(t) > 1]


def name_fingerprint(name):
    """Order-independent fingerprint of the significant tokens; catches small
    spelling differences and reordering in partner / company names."""
    toks = sorted(set(name_tokens(name)))
    return " ".join(toks)


def levenshtein(a, b):
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def names_similar(a, b, max_ratio=0.12):
    """True if two names are close (after fingerprinting) — small typos/reorder."""
    fa, fb = name_fingerprint(a), name_fingerprint(b)
    if not fa or not fb:
        return False
    if fa == fb:
        return True
    d = levenshtein(fa, fb)
    return d / max(len(fa), len(fb)) <= max_ratio


# --------------------------------------------------------------- geo ---------

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))
