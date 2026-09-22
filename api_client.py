"""Clients for the external services used by the platform.

- opencnpj.org  -> company registry lookups (free, no key)
- Nominatim     -> address -> lat/lon for the geographic map (free, 1 req/s policy)

Both are wrapped so the rest of the app deals only with clean, normalized dicts.
"""

import re
import time
import json
import unicodedata
import requests

import cnpj_utils
import enrich

OPENCNPJ_URL = "https://api.opencnpj.org/{cnpj}"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# A browser-like UA avoids Cloudflare bot-challenges some hosts apply to the
# public API. Nominatim, by contrast, asks for an identifying UA (set below).
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
NOMINATIM_UA = "cnpj-correlation-platform/1.0 (self-hosted fraud-analysis tool)"

PLACEHOLDER_CPF = "***000000**"


# ------------------------------------------------------------- normalizers ---

def clean_cnpj(raw):
    """Validate (length, charset, both check digits) and return the canonical
    14-char CNPJ, or None. Supports legacy numeric and 2026 alphanumeric CNPJs.
    Invalid numbers are rejected here so they never reach the fetch queue."""
    return cnpj_utils.clean_cnpj(raw)


def format_cnpj(raw):
    return cnpj_utils.format_cnpj(raw)


def normalize_name(name):
    if not name:
        return ""
    txt = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", txt).strip().upper()


def parse_capital(value):
    """'130000,00' -> 130000.0"""
    if value is None:
        return None
    s = str(value).strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def normalize_payload(raw):
    """Map a raw opencnpj JSON object into the flat shape the DB expects, plus
    normalized child collections (phones, emails, cnaes, address keys) used to
    populate the indexed correlation tables."""
    cnaes_raw = raw.get("cnaes") or []
    principal_desc = ""
    for c in cnaes_raw:
        if c.get("is_principal"):
            principal_desc = c.get("descricao", "")
            break

    partners = []
    for p in raw.get("QSA") or []:
        nome = p.get("nome_socio", "")
        partners.append({
            "nome_socio": nome,
            "nome_norm": normalize_name(nome),
            "nome_fp": enrich.name_fingerprint(nome),
            "cpf_cnpj_mask": p.get("cnpj_cpf_socio", ""),
            "qualificacao": p.get("qualificacao_socio", ""),
            "data_entrada": p.get("data_entrada_sociedade", ""),
            "faixa_etaria": p.get("faixa_etaria", ""),
        })

    # normalized phones
    phones_norm = []
    for t in raw.get("telefones") or []:
        pp = enrich.phone_parts(t.get("ddd"), t.get("numero"))
        if pp["full"]:
            pp["tipo"] = t.get("tipo", "")
            phones_norm.append(pp)

    # normalized e-mail(s) — the API exposes one, but model it as a collection
    emails_norm = []
    email = (raw.get("email") or "").strip().lower()
    if email:
        emails_norm.append(enrich.email_parts(email))

    # normalized CNAEs (principal + secondary). The live API exposes secondary
    # activities as a plain list of codes under "cnaes_secundarios" (no
    # descriptions) rather than as objects in "cnaes" (which is typically
    # empty) — both shapes are handled here so secondary CNAEs are never lost.
    principal_code = enrich.cnae_clean(raw.get("cnae_principal"))
    cnaes_norm = []
    seen_cnae = set()
    if principal_code:
        cnaes_norm.append({"codigo": principal_code, "descricao": principal_desc,
                           "is_principal": 1})
        seen_cnae.add(principal_code)
    for c in cnaes_raw:
        code = enrich.cnae_clean(c.get("codigo") or c.get("cnae"))
        if code and code not in seen_cnae:
            seen_cnae.add(code)
            cnaes_norm.append({"codigo": code, "descricao": c.get("descricao", ""),
                               "is_principal": 1 if c.get("is_principal") else 0})
    for code in (raw.get("cnaes_secundarios") or []):
        code = enrich.cnae_clean(code)
        if code and code not in seen_cnae:
            seen_cnae.add(code)
            cnaes_norm.append({"codigo": code, "descricao": "", "is_principal": 0})

    capital = parse_capital(raw.get("capital_social"))
    addr_keys = enrich.address_keys({
        "tipo_logradouro": raw.get("tipo_logradouro"),
        "logradouro": raw.get("logradouro"), "numero": raw.get("numero"),
        "cep": raw.get("cep"), "municipio": raw.get("municipio"), "uf": raw.get("uf"),
    })

    return {
        "razao_social": raw.get("razao_social", ""),
        "nome_fantasia": raw.get("nome_fantasia", ""),
        "situacao_cadastral": raw.get("situacao_cadastral", ""),
        "data_situacao": raw.get("data_situacao_cadastral", ""),
        "matriz_filial": raw.get("matriz_filial", ""),
        "data_inicio_atividade": raw.get("data_inicio_atividade", ""),
        "cnae_principal": raw.get("cnae_principal", ""),
        "cnae_principal_desc": principal_desc,
        "natureza_juridica": raw.get("natureza_juridica", ""),
        "tipo_logradouro": raw.get("tipo_logradouro", ""),
        "logradouro": raw.get("logradouro", ""),
        "numero": raw.get("numero", ""),
        "complemento": raw.get("complemento", ""),
        "bairro": raw.get("bairro", ""),
        "cep": raw.get("cep", ""),
        "uf": raw.get("uf", ""),
        "municipio": raw.get("municipio", ""),
        "email": email,
        "capital_social": capital,
        "capital_band": enrich.capital_band(capital),
        "porte_empresa": raw.get("porte_empresa", ""),
        "opcao_simples": raw.get("opcao_simples", ""),
        "opcao_mei": raw.get("opcao_mei", ""),
        "telefones": raw.get("telefones") or [],
        "qsa": raw.get("QSA") or [],
        "partners": partners,
        "phones_norm": phones_norm,
        "emails_norm": emails_norm,
        "cnaes_norm": cnaes_norm,
        "address_keys": addr_keys,
        "raw_json": json.dumps(raw, ensure_ascii=False),
    }


# ------------------------------------------------------------------ fetch ---

def fetch_cnpj(cnpj, timeout=20, retries=2):
    """Return (status, payload_or_None, error_msg).

    status is one of: 'ok', 'not_found', 'error'. Retries briefly on rate limits
    and transient network errors.
    """
    url = OPENCNPJ_URL.format(cnpj=cnpj)
    last_err = "unknown error"
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=timeout)
        except requests.RequestException as exc:
            last_err = f"network: {exc}"
            time.sleep(1.5 * (attempt + 1))
            continue

        if resp.status_code == 404:
            return "not_found", None, "CNPJ not found in registry"
        if resp.status_code == 429:
            last_err = "rate limited (429)"
            time.sleep(2.0 * (attempt + 1))
            continue
        if resp.status_code != 200:
            return "error", None, f"HTTP {resp.status_code}"

        try:
            raw = resp.json()
        except ValueError:
            return "error", None, "invalid JSON in response"
        if not raw or not raw.get("cnpj"):
            return "not_found", None, "empty record"
        return "ok", normalize_payload(raw), None

    return "error", None, last_err


# --------------------------------------------------------------- geocoding ---

def geocode_address(addr, timeout=20):
    """addr is a row dict from database.next_ungeocoded. Returns (lat, lon) or None."""
    parts = []
    if addr.get("logradouro"):
        line = " ".join(x for x in [addr.get("tipo_logradouro"), addr.get("logradouro")] if x)
        if addr.get("numero"):
            line += f", {addr['numero']}"
        parts.append(line)
    for key in ("bairro", "municipio", "uf"):
        if addr.get(key):
            parts.append(addr[key])
    parts.append("Brasil")
    query = ", ".join(parts)

    def _query(q, postalcode=None):
        params = {"q": q, "format": "json", "limit": 1, "countrycodes": "br"}
        if postalcode:
            params = {"postalcode": postalcode, "country": "Brazil",
                      "format": "json", "limit": 1}
        try:
            r = requests.get(NOMINATIM_URL, params=params,
                             headers={"User-Agent": NOMINATIM_UA}, timeout=timeout)
            if r.status_code == 200 and r.json():
                hit = r.json()[0]
                return float(hit["lat"]), float(hit["lon"])
        except (requests.RequestException, ValueError, KeyError, IndexError):
            return None
        return None

    result = _query(query)
    if result is None and addr.get("cep"):
        # fall back to the postal code centroid
        cep = re.sub(r"\D", "", addr["cep"])
        if len(cep) == 8:
            result = _query(None, postalcode=f"{cep[:5]}-{cep[5:]}")
    return result
