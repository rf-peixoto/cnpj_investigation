"""CNPJ correlation & fraud-analysis platform — Flask application.

Run:  python app.py   ->  http://127.0.0.1:5000
"""

import time
import threading

from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, abort, Response)

import database as db
import api_client as api
import correlations as corr

app = Flask(__name__)

# Politeness delays for the free external services.
FETCH_DELAY_S = 0.25      # opencnpj
GEOCODE_DELAY_S = 1.1     # Nominatim asks for <= 1 req/s


# ----------------------------------------------------------- background work ---

def fetch_worker():
    """Continuously drains pending CNPJs from the global pool."""
    while True:
        cnpj = db.next_pending_cnpj()
        if cnpj is None:
            time.sleep(1.0)
            continue
        status, payload, err = api.fetch_cnpj(cnpj)
        if status == "ok":
            db.save_cnpj_record(cnpj, payload)
        else:
            db.mark_cnpj_status(cnpj, status, err)
        time.sleep(FETCH_DELAY_S)


# Geocoding is triggered per-campaign and runs in its own short-lived thread so
# we never hammer Nominatim. Guard against launching duplicates.
_geocode_active = set()
_geocode_lock = threading.Lock()


def geocode_worker(campaign_id):
    try:
        while True:
            addr = db.next_ungeocoded(campaign_id)
            if addr is None:
                break
            result = api.geocode_address(addr)
            if result:
                db.save_geocode(addr["cnpj"], result[0], result[1], "ok")
            else:
                db.save_geocode(addr["cnpj"], None, None, "failed")
            time.sleep(GEOCODE_DELAY_S)
    finally:
        with _geocode_lock:
            _geocode_active.discard(campaign_id)


def start_geocoding(campaign_id):
    with _geocode_lock:
        if campaign_id in _geocode_active:
            return False
        _geocode_active.add(campaign_id)
    threading.Thread(target=geocode_worker, args=(campaign_id,), daemon=True).start()
    return True


# ---------------------------------------------------------------- ingestion ---

def ingest_raw_text(campaign_id, text):
    """Parse free text / file content into CNPJs and queue them. Returns counts."""
    added, invalid = 0, 0
    seen = set()
    # split on commas, semicolons, whitespace and newlines
    import re
    tokens = re.split(r"[\s,;]+", text or "")
    for tok in tokens:
        if not tok.strip():
            continue
        clean = api.clean_cnpj(tok)
        if clean and clean not in seen:
            seen.add(clean)
            db.add_cnpj_to_campaign(campaign_id, clean)
            added += 1
        elif not clean:
            invalid += 1
    return added, invalid


# -------------------------------------------------------------------- routes ---

@app.route("/")
def index():
    return render_template(
        "index.html",
        campaigns=db.list_campaigns(),
        stats=db.global_stats(),
    )


@app.route("/campaigns", methods=["POST"])
def create_campaign():
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("index"))
    cid = db.create_campaign(name, request.form.get("description", ""))
    return redirect(url_for("campaign", campaign_id=cid))


@app.route("/campaigns/<int:campaign_id>")
def campaign(campaign_id):
    c = db.get_campaign(campaign_id)
    if not c:
        abort(404)
    return render_template("campaign.html", campaign=c)


@app.route("/campaigns/<int:campaign_id>/cnpjs", methods=["POST"])
def add_cnpjs(campaign_id):
    if not db.get_campaign(campaign_id):
        abort(404)
    text = request.form.get("cnpjs", "")
    file = request.files.get("file")
    if file and file.filename:
        try:
            text += "\n" + file.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
    added, invalid = ingest_raw_text(campaign_id, text)
    return jsonify({"added": added, "invalid": invalid})


@app.route("/campaigns/<int:campaign_id>/remove", methods=["POST"])
def remove_cnpj(campaign_id):
    cnpj = api.clean_cnpj(request.form.get("cnpj", ""))
    if cnpj:
        db.remove_cnpj_from_campaign(campaign_id, cnpj)
    return jsonify({"ok": True})


@app.route("/campaigns/<int:campaign_id>/refresh", methods=["POST"])
def refresh_campaign(campaign_id):
    for row in db.campaign_cnpjs(campaign_id):
        db.reset_cnpj_for_refetch(row["cnpj"])
    return jsonify({"ok": True})


@app.route("/campaigns/<int:campaign_id>/geocode", methods=["POST"])
def geocode_campaign(campaign_id):
    started = start_geocoding(campaign_id)
    return jsonify({"started": started})


@app.route("/campaigns/<int:campaign_id>/delete", methods=["POST"])
def delete_campaign(campaign_id):
    db.delete_campaign(campaign_id)
    return redirect(url_for("index"))


@app.route("/campaigns/<int:campaign_id>/progress")
def progress(campaign_id):
    p = db.campaign_progress(campaign_id)
    with _geocode_lock:
        p["geocoding"] = campaign_id in _geocode_active
    return jsonify(p)


@app.route("/campaigns/<int:campaign_id>/data")
def campaign_data(campaign_id):
    if not db.get_campaign(campaign_id):
        abort(404)
    rows = db.campaign_cnpjs(campaign_id)
    analysis = corr.build_analysis(rows, global_partner_counts=db.global_partner_counts())
    # surface non-ok rows so the UI can show fetch problems
    problems = [
        {"cnpj": r["cnpj"], "status": r["fetch_status"], "error": r.get("fetch_error")}
        for r in rows if r["fetch_status"] not in ("ok", "pending", "fetching")
    ]
    analysis["problems"] = problems
    analysis["pending"] = sum(1 for r in rows if r["fetch_status"] in ("pending", "fetching"))
    return jsonify(analysis)


@app.route("/cnpj/<cnpj>")
def cnpj_detail(cnpj):
    cnpj = api.clean_cnpj(cnpj) or cnpj
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM cnpjs WHERE cnpj = ?", (cnpj,)).fetchone()
        if not row:
            abort(404)
        row = dict(row)
        campaigns = conn.execute(
            """SELECT c.id, c.name FROM campaigns c
               JOIN campaign_cnpjs cc ON cc.campaign_id = c.id
               WHERE cc.cnpj = ?""", (cnpj,)
        ).fetchall()
    import json as _json
    raw = _json.loads(row.get("raw_json") or "{}")
    return render_template("cnpj.html", row=row, raw=raw,
                           campaigns=[dict(c) for c in campaigns])


@app.route("/cnpj/<cnpj>/review", methods=["POST"])
def cnpj_review(cnpj):
    cnpj = api.clean_cnpj(cnpj) or cnpj
    db.set_cnpj_review(cnpj,
                       status=request.form.get("review_status"),
                       note=request.form.get("review_note"))
    return redirect(url_for("cnpj_detail", cnpj=cnpj))


@app.route("/partners")
def partners():
    try:
        min_companies = max(2, int(request.args.get("min", 2)))
    except (TypeError, ValueError):
        min_companies = 2
    return render_template("partners.html",
                           partners=db.prolific_partners(min_companies=min_companies),
                           min_companies=min_companies)


@app.route("/campaigns/<int:campaign_id>/export.csv")
def export_campaign(campaign_id):
    c = db.get_campaign(campaign_id)
    if not c:
        abort(404)
    rows = db.companies_for_export(campaign_id)
    # attach risk score + flags from a fresh analysis
    analysis = corr.build_analysis(db.campaign_cnpjs(campaign_id),
                                   global_partner_counts=db.global_partner_counts())
    risk_by = {r["cnpj"]: r for r in analysis["companies"]}
    import csv as _csv, io as _io
    buf = _io.StringIO()
    cols = ["cnpj", "razao_social", "nome_fantasia", "situacao_cadastral",
            "data_inicio_atividade", "cnae_principal", "cnae_principal_desc",
            "capital_social", "uf", "municipio", "bairro", "logradouro", "numero",
            "cep", "email", "socios", "review_status", "review_note",
            "risk_score", "risk_flags"]
    w = _csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        meta = risk_by.get(r["cnpj"], {})
        r["risk_score"] = meta.get("risk", "")
        r["risk_flags"] = "; ".join(meta.get("flags", []))
        w.writerow([r.get(k, "") for k in cols])
    fname = f"campaign_{campaign_id}_{c['name']}".replace(" ", "_")[:60]
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}.csv"'})


@app.route("/search")
def search():
    filters = {k: request.args.get(k, "") for k in
               ("q", "uf", "municipio", "bairro", "email", "cnae", "situacao", "socio")}
    filters["recent_only"] = request.args.get("recent_only") == "on"
    has_query = any(v for v in filters.values())
    results = db.search_cnpjs(filters) if has_query else []
    return render_template(
        "search.html", results=results, filters=filters, has_query=has_query,
        ufs=db.distinct_values("uf"),
        situacoes=db.distinct_values("situacao_cadastral"),
        campaigns=db.list_campaigns(),
    )


@app.template_filter("brl")
def brl(value):
    if value is None:
        return "—"
    try:
        return f"R$ {float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return "—"


def main():
    db.init_db()
    threading.Thread(target=fetch_worker, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)


if __name__ == "__main__":
    main()
