"""CNPJ correlation & fraud-analysis platform — Flask application.

Run:  python app.py   ->  http://127.0.0.1:5000
"""

import io
import os
import csv
import json
import time
import secrets
import threading
from urllib.parse import quote_plus

from flask import (Flask, render_template, request, redirect, url_for,
                   jsonify, abort, Response, session, send_file, g)
from werkzeug.utils import secure_filename

import database as db
import api_client as api
import correlations as corr

app = Flask(__name__)

# Session secret — used only to sign the CSRF token cookie for this local app.
# A fresh key each start is fine; it just invalidates any old open tab's token.
app.secret_key = os.environ.get("CNPJ_SECRET", secrets.token_hex(32))

# Uploads (evidence images). Kept outside the DB; restricted + size-capped.
UPLOAD_DIR = os.environ.get("CNPJ_UPLOADS", os.path.join(app.root_path, "uploads"))
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_EXT = {"png", "jpg", "jpeg"}
ALLOWED_MIME = {"image/png", "image/jpeg", "image/jpg"}
MAX_UPLOAD_BYTES = 8 * 1024 * 1024            # 8 MB per file
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# Politeness delays for the free external services.
FETCH_DELAY_S = 0.25      # opencnpj
GEOCODE_DELAY_S = 1.1     # Nominatim asks for <= 1 req/s


# ------------------------------------------------------------------- CSRF ---

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _csrf_token():
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf"] = tok
    return tok


@app.before_request
def _csrf_protect():
    if request.method in SAFE_METHODS:
        return
    sent = (request.form.get("csrf_token")
            or request.headers.get("X-CSRF-Token")
            or request.args.get("csrf_token"))
    if not sent or not secrets.compare_digest(str(sent), str(session.get("_csrf", ""))):
        abort(400, description="Invalid or missing CSRF token")


@app.context_processor
def _inject_csrf():
    return {"csrf_token": _csrf_token()}


@app.errorhandler(400)
def _bad_request(e):
    if request.path.endswith(("/data", "/progress")) or request.is_json \
            or request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"error": getattr(e, "description", "bad request")}), 400
    return Response(getattr(e, "description", "Bad request"), status=400)


@app.errorhandler(413)
def _too_large(e):
    return jsonify({"error": f"file too large (max {MAX_UPLOAD_BYTES // (1024*1024)} MB)"}), 413


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
        queue=db.fetch_queue_status(),
        watch_due=len(db.watchlist_due()),
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
    return render_template("campaign.html", campaign=c,
                           all_campaigns=db.list_campaigns())


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
    problems = [
        {"cnpj": r["cnpj"], "status": r["fetch_status"], "error": r.get("fetch_error")}
        for r in rows if r["fetch_status"] not in ("ok", "pending", "fetching")
    ]
    analysis["problems"] = problems
    analysis["pending"] = sum(1 for r in rows if r["fetch_status"] in ("pending", "fetching"))
    return jsonify(analysis)


# ------------------------------------------------- shortest path / why linked --

@app.route("/campaigns/<int:campaign_id>/path")
def campaign_path(campaign_id):
    if not db.get_campaign(campaign_id):
        abort(404)
    a = api.clean_cnpj(request.args.get("a", "")) or request.args.get("a", "")
    b = api.clean_cnpj(request.args.get("b", "")) or request.args.get("b", "")
    rows = db.campaign_cnpjs(campaign_id)
    return jsonify(corr.shortest_path(rows, a, b))


@app.route("/campaigns/<int:campaign_id>/why")
def campaign_why(campaign_id):
    if not db.get_campaign(campaign_id):
        abort(404)
    a = api.clean_cnpj(request.args.get("a", "")) or request.args.get("a", "")
    b = api.clean_cnpj(request.args.get("b", "")) or request.args.get("b", "")
    rows = db.campaign_cnpjs(campaign_id)
    return jsonify(corr.why_connected(rows, a, b))


# ----------------------------------------------------- campaign import/export --

@app.route("/campaigns/<int:campaign_id>/export.csv")
def export_campaign(campaign_id):
    c = db.get_campaign(campaign_id)
    if not c:
        abort(404)
    rows = db.companies_for_export(campaign_id)
    analysis = corr.build_analysis(db.campaign_cnpjs(campaign_id),
                                   global_partner_counts=db.global_partner_counts())
    risk_by = {r["cnpj"]: r for r in analysis["companies"]}
    buf = io.StringIO()
    cols = ["cnpj", "razao_social", "nome_fantasia", "situacao_cadastral",
            "data_inicio_atividade", "cnae_principal", "cnae_principal_desc",
            "capital_social", "uf", "municipio", "bairro", "logradouro", "numero",
            "cep", "email", "socios", "review_status", "review_note",
            "risk_score", "risk_flags"]
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        meta = risk_by.get(r["cnpj"], {})
        r["risk_score"] = meta.get("risk", "")
        r["risk_flags"] = "; ".join(meta.get("flags", []))
        w.writerow([r.get(k, "") for k in cols])
    fname = f"campaign_{campaign_id}_{c['name']}".replace(" ", "_")[:60]
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}.csv"'})


@app.route("/campaigns/<int:campaign_id>/export.json")
def export_campaign_json(campaign_id):
    data = db.campaign_export(campaign_id)
    if data is None:
        abort(404)
    fname = f"campaign_{campaign_id}".replace(" ", "_")[:60]
    return Response(json.dumps(data, ensure_ascii=False, indent=2),
                    mimetype="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{fname}.json"'})


@app.route("/campaigns/import", methods=["POST"])
def import_campaign():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "no file"}), 400
    raw = file.read().decode("utf-8", errors="ignore")
    name = file.filename.rsplit(".", 1)[0]
    if file.filename.lower().endswith(".json"):
        try:
            payload = json.loads(raw)
        except ValueError:
            return jsonify({"error": "invalid JSON"}), 400
        # accept either a full export dict or a bare list of CNPJs
        if isinstance(payload, list):
            payload = {"campaign": {"name": name}, "cnpjs": payload}
        elif "campaign" not in payload:
            payload = {"campaign": {"name": name},
                       "cnpjs": payload.get("cnpjs", []),
                       "records": payload.get("records", [])}
    else:  # treat as CSV / plain text — pull the cnpj column or any token
        cnpjs = []
        sniff = raw.splitlines()
        if sniff and "," in sniff[0]:
            reader = csv.DictReader(io.StringIO(raw))
            if reader.fieldnames and any("cnpj" in (f or "").lower() for f in reader.fieldnames):
                key = next(f for f in reader.fieldnames if "cnpj" in f.lower())
                cnpjs = [r.get(key, "") for r in reader]
            else:
                cnpjs = [c for line in sniff for c in line.split(",")]
        else:
            cnpjs = sniff
        payload = {"campaign": {"name": name}, "cnpjs": cnpjs}
    cid, added, invalid = db.campaign_import(payload)
    return jsonify({"campaign_id": cid, "added": added, "invalid": invalid})


@app.route("/campaigns/merge", methods=["POST"])
def merge_campaigns_route():
    try:
        src = int(request.form.get("src"))
        dst = int(request.form.get("dst"))
    except (TypeError, ValueError):
        return jsonify({"error": "bad ids"}), 400
    if src == dst:
        return jsonify({"error": "source and destination are the same"}), 400
    moved = db.merge_campaigns(src, dst)
    return jsonify({"moved": moved, "dst": dst})


# ----------------------------------------------------------- cnpj detail page --

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
    raw = json.loads(row.get("raw_json") or "{}")
    return render_template("cnpj.html", row=row, raw=raw,
                           cnpj_fmt=api.format_cnpj(cnpj),
                           campaigns=[dict(c) for c in campaigns],
                           attachments=db.list_attachments(cnpj),
                           watched=db.is_watched(cnpj),
                           history=db.cnpj_history(cnpj),
                           diff=db.cnpj_latest_diff(cnpj),
                           gmaps_url=google_maps_url(row),
                           cnaes=db.cnaes_for(cnpj))


def google_maps_url(row):
    """Build a Google Maps search link for a company's registered address, or
    None if there isn't enough address data to make one worthwhile."""
    street = " ".join(x for x in [row.get("tipo_logradouro"), row.get("logradouro")] if x)
    parts = [
        f"{street}, {row['numero']}" if street and row.get("numero") else street,
        row.get("bairro"), row.get("municipio"), row.get("uf"), row.get("cep"),
    ]
    parts = [p.strip() for p in parts if p and p.strip()]
    if not parts:
        return None
    query = quote_plus(", ".join(parts) + ", Brasil")
    return f"https://www.google.com/maps/search/?api=1&query={query}"


@app.route("/cnpj/<cnpj>/review", methods=["POST"])
def cnpj_review(cnpj):
    cnpj = api.clean_cnpj(cnpj) or cnpj
    db.set_cnpj_review(cnpj,
                       status=request.form.get("review_status"),
                       note=request.form.get("review_note"))
    return redirect(url_for("cnpj_detail", cnpj=cnpj))


# --------------------------------------------------------------- attachments ---

def _ext_ok(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


@app.route("/cnpj/<cnpj>/attachments", methods=["POST"])
def upload_attachment(cnpj):
    cnpj = api.clean_cnpj(cnpj) or cnpj
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "no file"}), 400
    if not _ext_ok(file.filename):
        return jsonify({"error": "only png, jpg, jpeg allowed"}), 400
    if file.mimetype not in ALLOWED_MIME:
        return jsonify({"error": f"unexpected content type {file.mimetype}"}), 400
    ext = file.filename.rsplit(".", 1)[1].lower()
    stored = secrets.token_hex(16) + "." + ext
    path = os.path.join(UPLOAD_DIR, stored)
    file.save(path)
    size = os.path.getsize(path)
    if size > MAX_UPLOAD_BYTES:
        os.remove(path)
        return jsonify({"error": "file too large"}), 413
    cid = request.form.get("campaign_id")
    cid = int(cid) if cid and cid.isdigit() else None
    att_id = db.add_attachment(cnpj, cid, secure_filename(file.filename), stored,
                               file.mimetype, size, request.form.get("caption", ""))
    return jsonify({"id": att_id, "filename": secure_filename(file.filename),
                    "stored_name": stored, "size": size,
                    "caption": request.form.get("caption", "")})


@app.route("/attachments/<int:att_id>/file")
def serve_attachment(att_id):
    att = db.get_attachment(att_id)
    if not att:
        abort(404)
    path = os.path.join(UPLOAD_DIR, att["stored_name"])
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype=att["mime"])


@app.route("/attachments/<int:att_id>/delete", methods=["POST"])
def delete_attachment_route(att_id):
    att = db.delete_attachment(att_id)
    if att:
        try:
            os.remove(os.path.join(UPLOAD_DIR, att["stored_name"]))
        except OSError:
            pass
    return jsonify({"ok": True})


# ----------------------------------------------------------------- watchlist ---

@app.route("/watchlist")
def watchlist():
    return render_template("watchlist.html", items=db.list_watchlist())


@app.route("/cnpj/<cnpj>/watch", methods=["POST"])
def watch_cnpj(cnpj):
    cnpj = api.clean_cnpj(cnpj) or cnpj
    action = request.form.get("action", "add")
    if action == "remove":
        db.remove_from_watchlist(cnpj)
        return jsonify({"watched": False})
    try:
        days = max(1, int(request.form.get("recheck_days", 30)))
    except (TypeError, ValueError):
        days = 30
    db.add_to_watchlist(cnpj, recheck_days=days, note=request.form.get("note", ""))
    return jsonify({"watched": True, "recheck_days": days})


@app.route("/watchlist/recheck", methods=["POST"])
def watchlist_recheck():
    due = db.watchlist_due()
    for c in due:
        db.reset_cnpj_for_refetch(c)
    return jsonify({"requeued": len(due)})


# --------------------------------------------------------------- queue / ops ---

@app.route("/queue")
def queue_page():
    return render_template("queue.html",
                           status=db.fetch_queue_status(),
                           errors=db.list_fetch_errors())


@app.route("/queue/status")
def queue_status():
    return jsonify({"status": db.fetch_queue_status(),
                    "errors": db.list_fetch_errors(limit=100)})


@app.route("/queue/requeue", methods=["POST"])
def queue_requeue():
    which = request.form.get("which", "errors")
    cid = request.form.get("campaign_id")
    cid = int(cid) if cid and cid.isdigit() else None
    n = db.requeue(which=which, campaign_id=cid)
    return jsonify({"requeued": n})


# -------------------------------------------------------------------- search ---

@app.route("/partners")
def partners():
    try:
        min_companies = max(2, int(request.args.get("min", 2)))
    except (TypeError, ValueError):
        min_companies = 2
    return render_template("partners.html",
                           partners=db.prolific_partners(min_companies=min_companies),
                           min_companies=min_companies)


@app.route("/search")
def search():
    filters = {k: request.args.get(k, "") for k in
               ("q", "uf", "municipio", "bairro", "email", "cnae", "situacao",
                "socio", "cep", "domain", "phone")}
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


@app.template_filter("cnpjfmt")
def cnpjfmt(value):
    return api.format_cnpj(value or "")


def main():
    db.init_db()
    threading.Thread(target=fetch_worker, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)


if __name__ == "__main__":
    main()
