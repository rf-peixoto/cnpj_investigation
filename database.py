"""SQLite persistence layer for the CNPJ consulting platform.

A single database file holds campaigns, the global pool of fetched CNPJ records,
the many-to-many membership between them, and a normalized partner (QSA) table
that makes owner-correlation queries fast.
"""

import os
import json
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("CNPJ_DB", os.path.join(os.path.dirname(__file__), "cnpj_platform.db"))

# Writes are serialized through this lock. WAL mode lets reads run concurrently.
_write_lock = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cnpjs (
    cnpj                  TEXT PRIMARY KEY,
    razao_social          TEXT,
    nome_fantasia         TEXT,
    situacao_cadastral    TEXT,
    data_situacao         TEXT,
    matriz_filial         TEXT,
    data_inicio_atividade TEXT,
    cnae_principal        TEXT,
    cnae_principal_desc   TEXT,
    natureza_juridica     TEXT,
    tipo_logradouro       TEXT,
    logradouro            TEXT,
    numero                TEXT,
    complemento           TEXT,
    bairro                TEXT,
    cep                   TEXT,
    uf                    TEXT,
    municipio             TEXT,
    email                 TEXT,
    capital_social        REAL,
    porte_empresa         TEXT,
    opcao_simples         TEXT,
    opcao_mei             TEXT,
    telefones             TEXT,   -- json array
    qsa                   TEXT,   -- json array
    raw_json              TEXT,
    fetch_status          TEXT DEFAULT 'pending',  -- pending|ok|not_found|error
    fetch_error           TEXT,
    fetched_at            TEXT,
    lat                   REAL,
    lon                   REAL,
    geocode_status        TEXT DEFAULT 'pending',  -- pending|ok|failed|skip
    review_status         TEXT DEFAULT 'none',     -- none|suspicious|cleared|confirmed
    review_note           TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS campaign_cnpjs (
    campaign_id INTEGER NOT NULL,
    cnpj        TEXT NOT NULL,
    added_at    TEXT NOT NULL,
    PRIMARY KEY (campaign_id, cnpj),
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS partners (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj          TEXT NOT NULL,
    nome_socio    TEXT,
    nome_norm     TEXT,
    cpf_cnpj_mask TEXT,
    qualificacao  TEXT,
    data_entrada  TEXT,
    faixa_etaria  TEXT,
    FOREIGN KEY (cnpj) REFERENCES cnpjs(cnpj) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_partners_cnpj ON partners(cnpj);
CREATE INDEX IF NOT EXISTS idx_partners_name ON partners(nome_norm);
CREATE INDEX IF NOT EXISTS idx_partners_cpf  ON partners(cpf_cnpj_mask);
CREATE INDEX IF NOT EXISTS idx_cnpjs_email   ON cnpjs(email);
CREATE INDEX IF NOT EXISTS idx_cnpjs_uf      ON cnpjs(uf);
CREATE INDEX IF NOT EXISTS idx_cc_cnpj       ON campaign_cnpjs(cnpj);
"""


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # lightweight migration for databases created before review columns existed
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(cnpjs)").fetchall()}
        if "review_status" not in cols:
            conn.execute("ALTER TABLE cnpjs ADD COLUMN review_status TEXT DEFAULT 'none'")
        if "review_note" not in cols:
            conn.execute("ALTER TABLE cnpjs ADD COLUMN review_note TEXT DEFAULT ''")


# ---------------------------------------------------------------- campaigns ---

def create_campaign(name, description=""):
    with _write_lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns (name, description, created_at) VALUES (?,?,?)",
            (name.strip(), description.strip(), now_iso()),
        )
        return cur.lastrowid


def list_campaigns():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM campaign_cnpjs cc WHERE cc.campaign_id = c.id) AS cnpj_count
            FROM campaigns c
            ORDER BY c.created_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_campaign(campaign_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        return dict(row) if row else None


def delete_campaign(campaign_id):
    with _write_lock, get_conn() as conn:
        conn.execute("DELETE FROM campaign_cnpjs WHERE campaign_id = ?", (campaign_id,))
        conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))


def rename_campaign(campaign_id, name, description):
    with _write_lock, get_conn() as conn:
        conn.execute(
            "UPDATE campaigns SET name = ?, description = ? WHERE id = ?",
            (name.strip(), description.strip(), campaign_id),
        )


# ----------------------------------------------------------- cnpj membership ---

def add_cnpj_to_campaign(campaign_id, cnpj):
    """Register a CNPJ in the global pool (status pending if new) and link it."""
    with _write_lock, get_conn() as conn:
        existing = conn.execute("SELECT cnpj FROM cnpjs WHERE cnpj = ?", (cnpj,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO cnpjs (cnpj, fetch_status) VALUES (?, 'pending')", (cnpj,)
            )
        conn.execute(
            "INSERT OR IGNORE INTO campaign_cnpjs (campaign_id, cnpj, added_at) VALUES (?,?,?)",
            (campaign_id, cnpj, now_iso()),
        )


def remove_cnpj_from_campaign(campaign_id, cnpj):
    with _write_lock, get_conn() as conn:
        conn.execute(
            "DELETE FROM campaign_cnpjs WHERE campaign_id = ? AND cnpj = ?",
            (campaign_id, cnpj),
        )


def reset_cnpj_for_refetch(cnpj):
    with _write_lock, get_conn() as conn:
        conn.execute(
            "UPDATE cnpjs SET fetch_status = 'pending', fetch_error = NULL WHERE cnpj = ?",
            (cnpj,),
        )


def campaign_cnpjs(campaign_id):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.*, cc.added_at AS added_at
            FROM campaign_cnpjs cc
            JOIN cnpjs c ON c.cnpj = cc.cnpj
            WHERE cc.campaign_id = ?
            ORDER BY cc.added_at ASC
            """,
            (campaign_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def campaign_progress(campaign_id):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.fetch_status AS status, COUNT(*) AS n
            FROM campaign_cnpjs cc
            JOIN cnpjs c ON c.cnpj = cc.cnpj
            WHERE cc.campaign_id = ?
            GROUP BY c.fetch_status
            """,
            (campaign_id,),
        ).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        total = sum(counts.values())
        done = total - counts.get("pending", 0)
        return {"total": total, "done": done, "counts": counts}


# ------------------------------------------------------------- fetch worker ---

def next_pending_cnpj():
    """Claim one pending CNPJ for fetching (sets it to 'fetching')."""
    with _write_lock, get_conn() as conn:
        row = conn.execute(
            "SELECT cnpj FROM cnpjs WHERE fetch_status = 'pending' LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE cnpjs SET fetch_status = 'fetching' WHERE cnpj = ?", (row["cnpj"],)
        )
        return row["cnpj"]


def save_cnpj_record(cnpj, data):
    """Persist a successful API payload + its normalized partner rows."""
    with _write_lock, get_conn() as conn:
        conn.execute(
            """
            UPDATE cnpjs SET
                razao_social=?, nome_fantasia=?, situacao_cadastral=?, data_situacao=?,
                matriz_filial=?, data_inicio_atividade=?, cnae_principal=?, cnae_principal_desc=?,
                natureza_juridica=?, tipo_logradouro=?, logradouro=?, numero=?, complemento=?,
                bairro=?, cep=?, uf=?, municipio=?, email=?, capital_social=?, porte_empresa=?,
                opcao_simples=?, opcao_mei=?, telefones=?, qsa=?, raw_json=?,
                fetch_status='ok', fetch_error=NULL, fetched_at=?
            WHERE cnpj=?
            """,
            (
                data["razao_social"], data["nome_fantasia"], data["situacao_cadastral"],
                data["data_situacao"], data["matriz_filial"], data["data_inicio_atividade"],
                data["cnae_principal"], data["cnae_principal_desc"], data["natureza_juridica"],
                data["tipo_logradouro"], data["logradouro"], data["numero"], data["complemento"],
                data["bairro"], data["cep"], data["uf"], data["municipio"], data["email"],
                data["capital_social"], data["porte_empresa"], data["opcao_simples"],
                data["opcao_mei"], json.dumps(data["telefones"], ensure_ascii=False),
                json.dumps(data["qsa"], ensure_ascii=False), data["raw_json"], now_iso(), cnpj,
            ),
        )
        conn.execute("DELETE FROM partners WHERE cnpj = ?", (cnpj,))
        for p in data["partners"]:
            conn.execute(
                """INSERT INTO partners
                   (cnpj, nome_socio, nome_norm, cpf_cnpj_mask, qualificacao, data_entrada, faixa_etaria)
                   VALUES (?,?,?,?,?,?,?)""",
                (cnpj, p["nome_socio"], p["nome_norm"], p["cpf_cnpj_mask"],
                 p["qualificacao"], p["data_entrada"], p["faixa_etaria"]),
            )


def mark_cnpj_status(cnpj, status, error=None):
    with _write_lock, get_conn() as conn:
        conn.execute(
            "UPDATE cnpjs SET fetch_status=?, fetch_error=?, fetched_at=? WHERE cnpj=?",
            (status, error, now_iso(), cnpj),
        )


# ---------------------------------------------------------------- geocoding ---

def next_ungeocoded(campaign_id):
    with _write_lock, get_conn() as conn:
        row = conn.execute(
            """
            SELECT c.cnpj, c.tipo_logradouro, c.logradouro, c.numero, c.bairro,
                   c.municipio, c.uf, c.cep
            FROM campaign_cnpjs cc
            JOIN cnpjs c ON c.cnpj = cc.cnpj
            WHERE cc.campaign_id = ? AND c.fetch_status = 'ok'
                  AND c.geocode_status = 'pending'
            LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE cnpjs SET geocode_status='fetching' WHERE cnpj=?", (row["cnpj"],)
        )
        return dict(row)


def save_geocode(cnpj, lat, lon, status):
    with _write_lock, get_conn() as conn:
        conn.execute(
            "UPDATE cnpjs SET lat=?, lon=?, geocode_status=? WHERE cnpj=?",
            (lat, lon, status, cnpj),
        )


# ------------------------------------------------------------ global search ---

def search_cnpjs(filters):
    """Filtered search across the entire pool (all campaigns)."""
    where, params = ["c.fetch_status = 'ok'"], []

    def like(col, val):
        where.append(f"{col} LIKE ?")
        params.append(f"%{val.strip()}%")

    if filters.get("q"):
        q = f"%{filters['q'].strip()}%"
        where.append(
            "(c.razao_social LIKE ? OR c.nome_fantasia LIKE ? OR c.cnpj LIKE ? "
            "OR c.email LIKE ? OR c.cnpj IN (SELECT cnpj FROM partners WHERE nome_norm LIKE ?))"
        )
        params.extend([q, q, q, q, q.upper()])
    if filters.get("uf"):
        where.append("c.uf = ?"); params.append(filters["uf"].strip().upper())
    if filters.get("municipio"):
        like("c.municipio", filters["municipio"])
    if filters.get("bairro"):
        like("c.bairro", filters["bairro"])
    if filters.get("email"):
        like("c.email", filters["email"])
    if filters.get("cnae"):
        like("c.cnae_principal", filters["cnae"])
    if filters.get("situacao"):
        where.append("c.situacao_cadastral = ?"); params.append(filters["situacao"].strip())
    if filters.get("socio"):
        where.append(
            "c.cnpj IN (SELECT cnpj FROM partners WHERE nome_norm LIKE ?)"
        )
        params.append(f"%{filters['socio'].strip().upper()}%")
    if filters.get("recent_only"):
        # data_inicio_atividade within last 365 days
        where.append("c.data_inicio_atividade >= date('now','-1 year')")
    if filters.get("campaign_id"):
        where.append("c.cnpj IN (SELECT cnpj FROM campaign_cnpjs WHERE campaign_id = ?)")
        params.append(filters["campaign_id"])

    sql = f"""
        SELECT c.*,
               (SELECT GROUP_CONCAT(nome_socio, ' | ') FROM partners p WHERE p.cnpj = c.cnpj) AS socios,
               (SELECT GROUP_CONCAT(DISTINCT cc.campaign_id) FROM campaign_cnpjs cc WHERE cc.cnpj = c.cnpj) AS campaign_ids
        FROM cnpjs c
        WHERE {' AND '.join(where)}
        ORDER BY c.razao_social
        LIMIT 500
    """
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def global_stats():
    with get_conn() as conn:
        def scalar(sql, *p):
            return conn.execute(sql, p).fetchone()[0]
        return {
            "campaigns": scalar("SELECT COUNT(*) FROM campaigns"),
            "cnpjs_total": scalar("SELECT COUNT(*) FROM cnpjs"),
            "cnpjs_ok": scalar("SELECT COUNT(*) FROM cnpjs WHERE fetch_status='ok'"),
            "cnpjs_pending": scalar(
                "SELECT COUNT(*) FROM cnpjs WHERE fetch_status IN ('pending','fetching')"
            ),
            "cnpjs_errors": scalar(
                "SELECT COUNT(*) FROM cnpjs WHERE fetch_status IN ('error','not_found')"
            ),
            "recent": scalar(
                "SELECT COUNT(*) FROM cnpjs WHERE fetch_status='ok' "
                "AND data_inicio_atividade >= date('now','-1 year')"
            ),
        }


def distinct_values(column):
    allowed = {"uf", "situacao_cadastral"}
    if column not in allowed:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {column} AS v FROM cnpjs "
            f"WHERE {column} IS NOT NULL AND {column} != '' ORDER BY v"
        ).fetchall()
        return [r["v"] for r in rows]


# ---------------------------------------------------- review / investigation ---

def set_cnpj_review(cnpj, status=None, note=None):
    """Update investigator review status and/or note for a CNPJ."""
    allowed = {"none", "suspicious", "cleared", "confirmed"}
    sets, params = [], []
    if status is not None and status in allowed:
        sets.append("review_status = ?"); params.append(status)
    if note is not None:
        sets.append("review_note = ?"); params.append(note.strip())
    if not sets:
        return
    params.append(cnpj)
    with _write_lock, get_conn() as conn:
        conn.execute(f"UPDATE cnpjs SET {', '.join(sets)} WHERE cnpj = ?", params)


def global_partner_counts(campaign_id=None):
    """Map partner-key -> number of distinct companies that partner owns.

    The key matches correlations._partners_full(): "<mask>::<UPPER NAME>" when a
    CPF/CNPJ mask is present, else the bare upper-cased name. Computed across the
    whole platform (or restricted to one campaign) so front-men who spread their
    companies across campaigns are still detected."""
    sql = """
        SELECT cpf_cnpj_mask AS mask, nome_norm AS name, COUNT(DISTINCT cnpj) AS n
        FROM partners
        {where}
        GROUP BY mask, name
    """
    where, params = "", []
    if campaign_id is not None:
        where = "WHERE cnpj IN (SELECT cnpj FROM campaign_cnpjs WHERE campaign_id = ?)"
        params.append(campaign_id)
    out = {}
    with get_conn() as conn:
        for r in conn.execute(sql.format(where=where), params).fetchall():
            name = (r["name"] or "").strip()
            mask = (r["mask"] or "").strip()
            if not name:
                continue
            key = f"{mask}::{name}" if mask and mask != "***000000**" else name
            out[key] = out.get(key, 0) + r["n"]
    return out


def prolific_partners(min_companies=2, limit=300):
    """Partners that own several companies platform-wide — serial-incorporator
    ('laranja') candidates — with the companies they are attached to."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.nome_socio AS name, p.cpf_cnpj_mask AS mask,
                   COUNT(DISTINCT p.cnpj) AS n,
                   GROUP_CONCAT(DISTINCT p.cnpj) AS cnpjs
            FROM partners p
            JOIN cnpjs c ON c.cnpj = p.cnpj AND c.fetch_status = 'ok'
            GROUP BY p.nome_norm, p.cpf_cnpj_mask
            HAVING n >= ?
            ORDER BY n DESC, name ASC
            LIMIT ?
            """,
            (min_companies, limit),
        ).fetchall()
        out = []
        for r in rows:
            cnpjs = (r["cnpjs"] or "").split(",")
            out.append({"name": r["name"], "mask": r["mask"], "count": r["n"],
                        "cnpjs": cnpjs})
        return out


def companies_for_export(campaign_id):
    """Flat rows for CSV export of a campaign (ok-fetched only)."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.cnpj, c.razao_social, c.nome_fantasia, c.situacao_cadastral,
                   c.data_inicio_atividade, c.cnae_principal, c.cnae_principal_desc,
                   c.capital_social, c.uf, c.municipio, c.bairro, c.logradouro,
                   c.numero, c.cep, c.email, c.review_status, c.review_note,
                   (SELECT GROUP_CONCAT(nome_socio, ' | ') FROM partners p
                    WHERE p.cnpj = c.cnpj) AS socios
            FROM campaign_cnpjs cc
            JOIN cnpjs c ON c.cnpj = cc.cnpj
            WHERE cc.campaign_id = ? AND c.fetch_status = 'ok'
            ORDER BY c.razao_social
            """,
            (campaign_id,),
        ).fetchall()
        return [dict(r) for r in rows]
