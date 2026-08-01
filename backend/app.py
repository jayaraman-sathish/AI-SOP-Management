import os
import re
import base64
import hashlib
import secrets
import datetime
import functools
import threading
import traceback

import jwt
from flask import Flask, request, jsonify, send_file, g, send_from_directory

import db
import ai_service
import docx_service
import summary_service
import report_service

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = db.DATA_DIR
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
GEN_DIR = os.path.join(DATA_DIR, "generated")
FRONTEND_DIR = os.path.join(BASE_DIR, "..", "frontend")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(GEN_DIR, exist_ok=True)

# In-memory job tracker for long-running RTM analysis runs (see run_rtm_mapping).
# Deliberately not persisted -- a job that was mid-run when the process restarts
# is simply gone, which is fine: the user just clicks "Run AI Gap Analysis"
# again and any already-committed RTM entries from before the restart are
# still there (each requirement commits as soon as it's assessed).
RTM_JOBS = {}

JWT_SECRET = os.environ.get("APP_JWT_SECRET", "dev-secret-change-me-in-production")

app = Flask(__name__)

# Cap total request body size so an oversized SOP package (large scanned annexures,
# embedded images, etc.) fails fast with a clear error instead of hanging until the
# gunicorn worker timeout hits, or exhausting memory on a small free-tier instance.
# 180MB covers a large multi-plant annexure/format package plus ~33% base64
# inflation. A single gunicorn worker (see render.yaml) keeps peak memory to one
# request's worth at a time on Render's 512MB free-tier instance -- this is
# already close to that instance's practical ceiling; if real SOP packages
# routinely exceed this, the real fix is a paid Render plan with more RAM
# (see README "Deploying"), not raising this number further.
app.config["MAX_CONTENT_LENGTH"] = 180 * 1024 * 1024


@app.errorhandler(413)
def too_large(_e):
    return jsonify({
        "error": "upload_too_large",
        "message": (
            "This SOP package is too large for the current server limit (180MB total, "
            "including base64 encoding overhead). Split large scanned annexures out, "
            "compress embedded images, or upload the oversized attachment separately."
        ),
    }), 413


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    """Last-resort catch-all so an unforeseen bug (e.g. a live AI call
    returning an unexpected shape somewhere not already guarded) surfaces as a
    clean, readable JSON error in the UI instead of a raw stack trace or a
    silently hung request. This does not replace fixing the root cause -- it
    just guarantees the failure mode is legible when something new slips
    through."""
    # Let Flask's own handling take over for HTTP-level exceptions (404, 413, etc.)
    # that already have a sensible default response -- only catch genuine bugs.
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("Unhandled error")
    db.log_error(
        "unhandled_exception",
        f"{type(e).__name__}: {e}",
        traceback_text=traceback.format_exc(),
        endpoint=f"{request.method} {request.path}",
    )
    return jsonify({
        "error": "internal_error",
        "message": f"Something went wrong processing that request ({type(e).__name__}: {e}). This has been logged to Admin > Error Log; try again, and if it keeps happening, it needs a code fix.",
    }), 500


@app.route("/", methods=["GET"])
def serve_frontend():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return resp


@app.route("/api/<path:_any>", methods=["OPTIONS"])
def cors_preflight(_any):
    return ("", 204)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${h}"


def check_password(password, stored):
    try:
        salt, h = stored.split("$")
    except ValueError:
        return False
    return hashlib.sha256((salt + password).encode()).hexdigest() == h


def make_token(user):
    payload = {
        "uid": user["id"],
        "username": user["username"],
        "role": user["role"],
        "site_id": user["site_id"],
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=12),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def require_auth(roles=None):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return jsonify({"error": "missing_token"}), 401
            token = auth.split(" ", 1)[1]
            try:
                payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            except jwt.PyJWTError:
                return jsonify({"error": "invalid_token"}), 401
            if roles and payload["role"] not in roles:
                return jsonify({"error": "forbidden", "required_roles": roles}), 403
            g.user = payload
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def actor_label():
    u = getattr(g, "user", None)
    return f"{u['username']} ({u['role']})" if u else "system"


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json(force=True)
    conn = db.get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (data.get("username", ""),)).fetchone()
    conn.close()
    if not row or not check_password(data.get("password", ""), row["password_hash"]):
        return jsonify({"error": "invalid_credentials"}), 401
    token = make_token(row)
    db.log_audit(row["username"], "login", "user", row["id"])
    return jsonify({
        "token": token,
        "user": {"id": row["id"], "username": row["username"], "name": row["name"], "role": row["role"], "site_id": row["site_id"]},
    })


@app.route("/api/auth/me", methods=["GET"])
@require_auth()
def me():
    conn = db.get_db()
    row = conn.execute("SELECT id, username, name, role, site_id FROM users WHERE id = ?", (g.user["uid"],)).fetchone()
    conn.close()
    return jsonify(db.row_to_dict(row))


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------
@app.route("/api/sites", methods=["GET"])
@require_auth()
def list_sites():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM sites ORDER BY code").fetchall()
    conn.close()
    return jsonify(db.rows_to_list(rows))


@app.route("/api/error-log", methods=["GET"])
@require_auth(roles=["admin"])
def get_error_log():
    """Admin-only view of recent server-side errors -- lets the user (or
    Claude, if they paste this back) see exactly what went wrong without
    needing access to Render's own server console, which isn't something
    Claude can reach directly."""
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM error_log ORDER BY id DESC LIMIT 100").fetchall()
    conn.close()
    return jsonify({"errors": db.rows_to_list(rows)})


@app.route("/api/sites", methods=["POST"])
@require_auth(roles=["admin"])
def create_site():
    data = request.get_json(force=True)
    conn = db.get_db()
    cur = conn.execute(
        "INSERT INTO sites (code, name, location, product_type, sterilization_method, markets, notes, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (data["code"], data["name"], data.get("location", ""), data.get("product_type", ""),
         data.get("sterilization_method", ""), data.get("markets", ""), data.get("notes", ""), db.now()),
    )
    conn.commit()
    site_id = cur.lastrowid
    conn.close()
    db.log_audit(actor_label(), "create", "site", site_id, data)
    return jsonify({"id": site_id}), 201


@app.route("/api/sites/<int:site_id>", methods=["DELETE"])
@require_auth(roles=["admin"])
def delete_site(site_id):
    """Removes a site (e.g. one of the seeded demo sites) once it's no longer
    needed. Refuses to delete a site that has SOPs uploaded against it, so a
    misclick can't silently orphan real data -- reassign or delete those SOPs
    first if you really need to remove a site that has content."""
    conn = db.get_db()
    site = conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
    if not site:
        conn.close()
        return jsonify({"error": "not_found"}), 404
    sop_count = conn.execute("SELECT COUNT(*) c FROM sops WHERE site_id=?", (site_id,)).fetchone()["c"]
    if sop_count > 0:
        conn.close()
        return jsonify({
            "error": "site_has_sops",
            "message": f"This site has {sop_count} SOP(s) uploaded against it -- delete or reassign those first before removing the site.",
        }), 400
    conn.execute("DELETE FROM rtm_entries WHERE site_id=?", (site_id,))
    conn.execute("DELETE FROM gaps WHERE site_id=?", (site_id,))
    conn.execute("DELETE FROM sites WHERE id=?", (site_id,))
    conn.commit()
    conn.close()
    db.log_audit(actor_label(), "delete", "site", site_id, {"code": site["code"], "name": site["name"]})
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Requirements (regulatory library)
# ---------------------------------------------------------------------------
@app.route("/api/requirements", methods=["GET"])
@require_auth()
def list_requirements():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM requirements ORDER BY id").fetchall()
    conn.close()
    return jsonify(db.rows_to_list(rows))


# ---------------------------------------------------------------------------
# SOPs
# ---------------------------------------------------------------------------
@app.route("/api/sops", methods=["GET"])
@require_auth()
def list_sops():
    site_id = request.args.get("site_id")
    conn = db.get_db()
    q = """
        SELECT s.*, sv.id as current_version_id, sv.version_label, sv.filename, sv.uploaded_at,
               (SELECT COUNT(*) FROM sop_attachments sa WHERE sa.sop_version_id = sv.id) as attachment_count
        FROM sops s
        LEFT JOIN sop_versions sv ON sv.sop_id = s.id AND sv.is_current = 1
    """
    params = []
    if site_id:
        q += " WHERE s.site_id = ?"
        params.append(site_id)
    q += " ORDER BY s.sop_number"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify(db.rows_to_list(rows))


@app.route("/api/sops", methods=["POST"])
@require_auth(roles=["admin", "analyst", "site_owner"])
def upload_sop():
    """
    Accepts JSON: {site_id, sop_number, title, process_area, sop_category,
                   version_label, filename, file_base64}
    file_base64 is the raw .docx content, base64-encoded (avoids needing a
    multipart upload library that isn't available in this environment).

    Accepts a package of one or more files under "files": a real SOP is rarely
    just one document -- it typically comes with annexures and format/template
    attachments. Exactly one file should be tagged doc_type "Main SOP" (the
    procedure itself, the one that gets redlined); everything else is stored
    as a linked attachment (Annexure / Format / Other) and its text is folded
    into AI analysis alongside the main document, so gap detection considers
    the whole SOP package, not just the primary file. If no file is explicitly
    tagged "Main SOP", the first file in the list is used as the main document.

    For backward compatibility, a single-file payload (filename + file_base64
    at the top level, no "files" array) is still accepted and treated as a
    one-file package.
    """
    data = request.get_json(force=True)
    files_payload = data.get("files")
    if not files_payload:
        # Legacy single-file shape.
        if not data.get("filename") or not data.get("file_base64"):
            return jsonify({"error": "missing_fields", "fields": ["files (or legacy filename/file_base64)"]}), 400
        files_payload = [{"filename": data["filename"], "file_base64": data["file_base64"], "doc_type": "Main SOP"}]

    required = ["site_id", "sop_number", "title"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": "missing_fields", "fields": missing}), 400
    if not isinstance(files_payload, list) or len(files_payload) == 0:
        return jsonify({"error": "files_must_be_a_non_empty_array"}), 400

    main_files = [f for f in files_payload if (f.get("doc_type") or "").strip().lower() == "main sop"]
    main_file = main_files[0] if main_files else files_payload[0]
    other_files = [f for f in files_payload if f is not main_file]

    conn = db.get_db()
    sop = conn.execute("SELECT * FROM sops WHERE site_id=? AND sop_number=?", (data["site_id"], data["sop_number"])).fetchone()
    if sop is None:
        cur = conn.execute(
            "INSERT INTO sops (site_id, sop_number, title, process_area, sop_category, status, created_at) VALUES (?,?,?,?,?, 'active', ?)",
            (data["site_id"], data["sop_number"], data["title"], data.get("process_area", ""), data.get("sop_category", ""), db.now()),
        )
        sop_id = cur.lastrowid
    else:
        sop_id = sop["id"]
        conn.execute("UPDATE sops SET title=?, process_area=?, sop_category=? WHERE id=?",
                     (data["title"], data.get("process_area", ""), data.get("sop_category", ""), sop_id))
        conn.execute("UPDATE sop_versions SET is_current = 0 WHERE sop_id = ?", (sop_id,))

    def _save_file(file_item):
        try:
            file_bytes = base64.b64decode(file_item["file_base64"])
        except Exception:
            raise ValueError(f"invalid_base64: {file_item.get('filename')}")
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", file_item["filename"])
        stored_name = f"sop{sop_id}_{secrets.token_hex(4)}_{safe_name}"
        filepath = os.path.join(UPLOAD_DIR, stored_name)
        with open(filepath, "wb") as f:
            f.write(file_bytes)
        extracted_text = docx_service.extract_text(filepath) if filepath.lower().endswith(".docx") else ""
        return filepath, extracted_text

    try:
        main_filepath, main_extracted_text = _save_file(main_file)
    except ValueError as e:
        conn.close()
        return jsonify({"error": str(e)}), 400

    version_label = data.get("version_label") or "v1.0"
    cur = conn.execute(
        "INSERT INTO sop_versions (sop_id, version_label, filename, filepath, extracted_text, is_current, uploaded_by, uploaded_at) VALUES (?,?,?,?,?,1,?,?)",
        (sop_id, version_label, main_file["filename"], main_filepath, main_extracted_text, actor_label(), db.now()),
    )
    version_id = cur.lastrowid

    attachment_count = 0
    for f in other_files:
        try:
            fpath, ftext = _save_file(f)
        except ValueError:
            continue  # skip a bad attachment rather than failing the whole upload
        doc_type = (f.get("doc_type") or "Annexure").strip() or "Annexure"
        conn.execute(
            "INSERT INTO sop_attachments (sop_version_id, doc_type, filename, filepath, extracted_text, uploaded_by, uploaded_at) VALUES (?,?,?,?,?,?,?)",
            (version_id, doc_type, f["filename"], fpath, ftext, actor_label(), db.now()),
        )
        attachment_count += 1

    conn.commit()
    conn.close()

    db.log_audit(actor_label(), "upload", "sop_version", version_id, {
        "sop_id": sop_id, "version_label": version_label, "attachments": attachment_count,
    })
    return jsonify({
        "sop_id": sop_id, "version_id": version_id,
        "extracted_chars": len(main_extracted_text), "attachments_saved": attachment_count,
    }), 201


@app.route("/api/sops/<int:sop_id>", methods=["GET"])
@require_auth()
def get_sop(sop_id):
    conn = db.get_db()
    sop = conn.execute("SELECT * FROM sops WHERE id=?", (sop_id,)).fetchone()
    if not sop:
        conn.close()
        return jsonify({"error": "not_found"}), 404
    versions = conn.execute("SELECT id, version_label, filename, uploaded_by, uploaded_at, is_current FROM sop_versions WHERE sop_id=? ORDER BY id DESC", (sop_id,)).fetchall()
    version_list = db.rows_to_list(versions)
    for v in version_list:
        attachments = conn.execute(
            "SELECT id, doc_type, filename, uploaded_by, uploaded_at FROM sop_attachments WHERE sop_version_id=? ORDER BY id",
            (v["id"],),
        ).fetchall()
        v["attachments"] = db.rows_to_list(attachments)
    conn.close()
    result = db.row_to_dict(sop)
    result["versions"] = version_list
    return jsonify(result)


@app.route("/api/sops/<int:sop_id>", methods=["PATCH"])
@require_auth(roles=["admin", "analyst", "site_owner"])
def update_sop_metadata(sop_id):
    """Edit an SOP's title/process_area/sop_category without re-uploading files.
    Added specifically so a category can be set/corrected after the fact --
    Site Comparison and RTM matching both key off sop_category, and real-world
    uploads don't always get it right (or at all) on the first pass."""
    data = request.get_json(force=True)
    conn = db.get_db()
    sop = conn.execute("SELECT * FROM sops WHERE id=?", (sop_id,)).fetchone()
    if not sop:
        conn.close()
        return jsonify({"error": "not_found"}), 404

    title = data.get("title", sop["title"])
    process_area = data.get("process_area", sop["process_area"])
    sop_category = data.get("sop_category", sop["sop_category"])
    if not (title or "").strip():
        conn.close()
        return jsonify({"error": "title_required"}), 400

    conn.execute(
        "UPDATE sops SET title=?, process_area=?, sop_category=? WHERE id=?",
        (title.strip(), (process_area or "").strip(), (sop_category or "").strip(), sop_id),
    )
    conn.commit()
    conn.close()

    db.log_audit(actor_label(), "edit_metadata", "sop", sop_id, {
        "title": title, "process_area": process_area, "sop_category": sop_category,
    })
    return jsonify({"ok": True})


@app.route("/api/sop_versions/<int:version_id>/download", methods=["GET"])
@require_auth()
def download_sop_version(version_id):
    """Download the original uploaded main SOP document for a given version
    (not a redline -- the file exactly as uploaded, so you can open/verify it)."""
    conn = db.get_db()
    version = conn.execute("SELECT * FROM sop_versions WHERE id=?", (version_id,)).fetchone()
    conn.close()
    if not version or not os.path.exists(version["filepath"]):
        return jsonify({"error": "file_not_found"}), 404
    return send_file(version["filepath"], as_attachment=True, download_name=version["filename"])


@app.route("/api/sop_attachments/<int:attachment_id>/download", methods=["GET"])
@require_auth()
def download_sop_attachment(attachment_id):
    """Download an original uploaded annexure/format attachment exactly as uploaded."""
    conn = db.get_db()
    attachment = conn.execute("SELECT * FROM sop_attachments WHERE id=?", (attachment_id,)).fetchone()
    conn.close()
    if not attachment or not os.path.exists(attachment["filepath"]):
        return jsonify({"error": "file_not_found"}), 404
    return send_file(attachment["filepath"], as_attachment=True, download_name=attachment["filename"])


@app.route("/api/sop_versions/<int:version_id>/attachments", methods=["POST"])
@require_auth(roles=["admin", "analyst", "site_owner"])
def add_sop_attachments(version_id):
    """Adds one or more annexure/format files to an SOP *after* the initial
    upload -- e.g. you uploaded just the main SOP and now have the annexures
    to hand, or realized one was missing. Does not touch the main document or
    require re-uploading anything already there."""
    conn = db.get_db()
    version = conn.execute("SELECT * FROM sop_versions WHERE id=?", (version_id,)).fetchone()
    if not version:
        conn.close()
        return jsonify({"error": "version_not_found"}), 404

    data = request.get_json(force=True)
    files_payload = data.get("files")
    if not isinstance(files_payload, list) or len(files_payload) == 0:
        conn.close()
        return jsonify({"error": "files_must_be_a_non_empty_array"}), 400

    sop_id = version["sop_id"]
    saved = 0
    for f in files_payload:
        try:
            file_bytes = base64.b64decode(f["file_base64"])
        except Exception:
            continue  # skip a bad file rather than failing the whole batch
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", f.get("filename") or "attachment.docx")
        stored_name = f"sop{sop_id}_{secrets.token_hex(4)}_{safe_name}"
        filepath = os.path.join(UPLOAD_DIR, stored_name)
        with open(filepath, "wb") as out:
            out.write(file_bytes)
        extracted_text = docx_service.extract_text(filepath) if filepath.lower().endswith(".docx") else ""
        doc_type = (f.get("doc_type") or "Annexure").strip() or "Annexure"
        conn.execute(
            "INSERT INTO sop_attachments (sop_version_id, doc_type, filename, filepath, extracted_text, uploaded_by, uploaded_at) VALUES (?,?,?,?,?,?,?)",
            (version_id, doc_type, f.get("filename") or safe_name, filepath, extracted_text, actor_label(), db.now()),
        )
        saved += 1

    conn.commit()
    conn.close()
    if saved == 0:
        return jsonify({"error": "no_valid_files"}), 400
    db.log_audit(actor_label(), "add_attachments", "sop_version", version_id, {"sop_id": sop_id, "attachments_added": saved})
    return jsonify({"attachments_saved": saved}), 201


# ---------------------------------------------------------------------------
# Discovery: AI-suggested requirements not in the curated library (unverified
# candidates for human review -- never an official RTM finding on their own)
# ---------------------------------------------------------------------------
@app.route("/api/sops/<int:sop_id>/discover-gaps", methods=["POST"])
@require_auth(roles=["admin", "analyst", "quality_lead"])
def discover_gaps(sop_id):
    conn = db.get_db()
    sop = conn.execute("SELECT * FROM sops WHERE id=?", (sop_id,)).fetchone()
    if not sop:
        conn.close()
        return jsonify({"error": "sop_not_found"}), 404
    full_text = db.full_sop_text(conn, sop_id)
    if not full_text:
        conn.close()
        return jsonify({"error": "sop_has_no_extracted_text"}), 400
    existing = conn.execute(
        "SELECT source, clause, requirement_text FROM requirements WHERE sop_category=?", (sop["sop_category"],)
    ).fetchall()
    existing_summaries = [f"{r['source']} {r['clause']}: {r['requirement_text'][:100]}" for r in existing]

    result = ai_service.discover_uncovered_topics(sop["title"], sop["sop_category"], full_text, existing_summaries)

    saved_ids = []
    for c in result["candidates"]:
        cur = conn.execute(
            "INSERT INTO discovery_candidates (sop_id, site_id, topic, suggested_source, suggested_clause, "
            "suggested_category, rationale, status, created_at) VALUES (?,?,?,?,?,?,?, 'New', ?)",
            (
                sop_id, sop["site_id"], c.get("topic", ""), c.get("suggested_source", ""),
                c.get("suggested_clause", ""), c.get("suggested_category", ""), c.get("rationale", ""), db.now(),
            ),
        )
        saved_ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    db.log_audit(actor_label(), "discover_gaps", "sop", sop_id, {"candidates_found": len(saved_ids), "ai_mock": result["ai_mock"]})
    return jsonify({
        "candidates_found": len(saved_ids),
        "ai_mock": result["ai_mock"],
        "offline_note": result.get("offline_note"),
    })


@app.route("/api/discovery-candidates", methods=["GET"])
@require_auth()
def list_discovery_candidates():
    site_id = request.args.get("site_id")
    conn = db.get_db()
    q = """
        SELECT dc.*, sop.sop_number, sop.title as sop_title, s.code as site_code
        FROM discovery_candidates dc
        JOIN sops sop ON sop.id = dc.sop_id
        JOIN sites s ON s.id = dc.site_id
    """
    params = []
    if site_id:
        q += " WHERE dc.site_id = ?"
        params.append(site_id)
    q += " ORDER BY dc.id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify(db.rows_to_list(rows))


@app.route("/api/discovery-candidates/<int:cand_id>/dismiss", methods=["POST"])
@require_auth(roles=["admin", "quality_lead"])
def dismiss_discovery_candidate(cand_id):
    conn = db.get_db()
    cand = conn.execute("SELECT id FROM discovery_candidates WHERE id=?", (cand_id,)).fetchone()
    if not cand:
        conn.close()
        return jsonify({"error": "not_found"}), 404
    conn.execute(
        "UPDATE discovery_candidates SET status='Dismissed', reviewed_by=?, reviewed_at=? WHERE id=?",
        (actor_label(), db.now(), cand_id),
    )
    conn.commit()
    conn.close()
    db.log_audit(actor_label(), "dismiss_discovery_candidate", "discovery_candidate", cand_id)
    return jsonify({"ok": True})


@app.route("/api/discovery-candidates/<int:cand_id>/promote", methods=["POST"])
@require_auth(roles=["admin", "quality_lead"])
def promote_discovery_candidate(cand_id):
    """Turns a reviewed candidate into a real, official requirement in the
    curated library -- the only way one of these ever becomes something RTM
    actually checks against. The reviewer can override any field; whatever
    they submit (or the original AI suggestion, if left blank) is what gets
    stored, so this is the human-vetting step, not an automatic promotion."""
    data = request.get_json(force=True)
    conn = db.get_db()
    cand = conn.execute("SELECT * FROM discovery_candidates WHERE id=?", (cand_id,)).fetchone()
    if not cand:
        conn.close()
        return jsonify({"error": "not_found"}), 404
    if cand["status"] == "Promoted":
        conn.close()
        return jsonify({"error": "already_promoted", "requirement_id": cand["promoted_requirement_id"]}), 400

    req_code = f"REQ-{secrets.token_hex(3).upper()}"
    source = (data.get("source") or cand["suggested_source"] or "Custom").strip()
    clause = (data.get("clause") or cand["suggested_clause"] or "").strip()
    category = (data.get("sop_category") or cand["suggested_category"] or "").strip()
    process_area = (data.get("process_area") or cand["topic"] or "").strip()
    requirement_text = (data.get("requirement_text") or cand["rationale"] or cand["topic"] or "").strip()
    if not category:
        conn.close()
        return jsonify({"error": "sop_category_required"}), 400

    cur = conn.execute(
        "INSERT INTO requirements (req_code, source, clause, process_area, requirement_text, sop_category, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (req_code, source, clause, process_area, requirement_text, category, db.now()),
    )
    new_req_id = cur.lastrowid
    conn.execute(
        "UPDATE discovery_candidates SET status='Promoted', reviewed_by=?, reviewed_at=?, promoted_requirement_id=? WHERE id=?",
        (actor_label(), db.now(), new_req_id, cand_id),
    )
    conn.commit()
    conn.close()
    db.log_audit(actor_label(), "promote_discovery_candidate", "discovery_candidate", cand_id, {"new_requirement_id": new_req_id, "req_code": req_code})
    return jsonify({"ok": True, "requirement_id": new_req_id, "req_code": req_code})


# ---------------------------------------------------------------------------
# RTM: AI-assisted requirement-to-SOP mapping
# ---------------------------------------------------------------------------
@app.route("/api/rtm", methods=["GET"])
@require_auth()
def list_rtm():
    site_id = request.args.get("site_id")
    conn = db.get_db()
    q = """
        SELECT r.id, r.coverage_status, r.rationale, r.cited_text, r.ai_proposed, r.ai_mock,
               r.confirmed_by, r.confirmed_at, r.updated_at,
               req.id as requirement_id, req.req_code, req.source, req.clause, req.process_area,
               req.requirement_text, req.sop_category,
               s.id as site_id, s.code as site_code, s.name as site_name,
               sop.id as sop_id, sop.sop_number, sop.title as sop_title
        FROM rtm_entries r
        JOIN requirements req ON req.id = r.requirement_id
        JOIN sites s ON s.id = r.site_id
        LEFT JOIN sops sop ON sop.id = r.sop_id
    """
    params = []
    if site_id:
        q += " WHERE r.site_id = ?"
        params.append(site_id)
    q += " ORDER BY req.id"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify(db.rows_to_list(rows))


def _run_rtm_job(job_id, site_id, requested_sop_ids, actor):
    """Background worker for an RTM run -- see run_rtm_mapping for why this
    doesn't run inline in the request. Runs in its own thread with its own DB
    connection (sqlite3 connections aren't shared across threads); updates
    RTM_JOBS[job_id] as it goes so the frontend can poll progress."""
    job = RTM_JOBS[job_id]
    conn = db.get_db()
    try:
        site = conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
        if not site:
            job.update(status="error", error="site_not_found")
            return

        requirements = conn.execute("SELECT * FROM requirements ORDER BY id").fetchall()
        sop_rows = conn.execute("""
            SELECT sop.id as sop_id, sop.sop_number, sop.title, sop.sop_category
            FROM sops sop JOIN sop_versions sv ON sv.sop_id = sop.id AND sv.is_current = 1
            WHERE sop.site_id = ?
        """, (site_id,)).fetchall()
        sops = db.rows_to_list(sop_rows)
        # Whether this run considered every SOP currently uploaded at the site,
        # vs. being scoped to a specific subset (the normal case now -- the
        # per-SOP "Check Compliance" button always passes exactly one sop_id).
        # This matters for what "SOP Missing" is allowed to mean below.
        is_full_site_run = not requested_sop_ids
        if requested_sop_ids:
            requested_set = {int(i) for i in requested_sop_ids}
            sops = [s for s in sops if s["sop_id"] in requested_set]
            if not sops:
                job.update(status="error", error="no_matching_sops")
                return
        for s in sops:
            s["full_text"] = db.full_sop_text(conn, s["sop_id"])

        job.update(total=len(requirements), done=0)
        results = []
        failed = []
        for req in requirements:
            req_d = db.row_to_dict(req)
            try:
                if not sops:
                    # Nothing at all has been uploaded/checked for this site --
                    # a cheap, honest shortcut that doesn't need an AI call.
                    assessment = {
                        "coverage_status": "SOP Missing",
                        "rationale": "No SOP has been uploaded (or checked) for this site at all.",
                        "cited_text": "",
                        "sop_id": None,
                        "ai_mock": True,
                    }
                else:
                    # Matching is content-driven, not category-tag-driven: every
                    # currently-checked SOP is passed as a candidate for every
                    # requirement, and the model itself judges relevance from
                    # actual text rather than trusting a category label upstream.
                    # This means a mistagged or partially-relevant SOP can still
                    # get matched correctly, and it means a requirement whose
                    # topic none of the checked SOPs actually address gets an
                    # honest "SOP Missing" (decided by the model reading the
                    # content) instead of either a false "Not Covered" against an
                    # unrelated document or a missed match from a wrong tag.
                    excerpts = [{"sop_id": s["sop_id"], "sop_number": s["sop_number"], "title": s["title"], "text": s["full_text"]} for s in sops]
                    assessment = ai_service.assess_requirement_coverage(req_d, excerpts)

                existing = conn.execute("SELECT id FROM rtm_entries WHERE requirement_id=? AND site_id=?", (req_d["id"], site_id)).fetchone()
                sop_id = assessment.get("sop_id")
                if sop_id is None and assessment["coverage_status"] != "SOP Missing" and len(sops) == 1:
                    # Check Compliance is normally scoped to a single SOP (the per-SOP
                    # "Check Compliance" button). If the model judged this requirement
                    # relevant enough to not say "SOP Missing" but forgot to echo the
                    # sop_id field back, there's no real ambiguity -- it can only be
                    # this one SOP. Filling it in here means Generate Redline later
                    # doesn't dead-end on a target that was obvious from context.
                    sop_id = sops[0]["sop_id"]
                if existing:
                    conn.execute(
                        "UPDATE rtm_entries SET coverage_status=?, rationale=?, cited_text=?, ai_proposed=?, ai_mock=?, updated_at=?, confirmed_by=NULL, confirmed_at=NULL WHERE requirement_id=? AND site_id=?",
                        (assessment["coverage_status"], assessment.get("rationale", ""), assessment.get("cited_text", ""), 1, int(bool(assessment.get("ai_mock"))), db.now(), req_d["id"], site_id),
                    )
                    entry_id = existing["id"]
                    conn.execute("UPDATE rtm_entries SET sop_id=? WHERE id=?", (sop_id, entry_id))
                else:
                    cur = conn.execute(
                        "INSERT INTO rtm_entries (requirement_id, site_id, sop_id, coverage_status, rationale, cited_text, ai_proposed, ai_mock, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (req_d["id"], site_id, sop_id, assessment["coverage_status"], assessment.get("rationale", ""), assessment.get("cited_text", ""), 1, int(bool(assessment.get("ai_mock"))), db.now()),
                    )
                    entry_id = cur.lastrowid

                # Auto-create/refresh a gap record for anything not fully covered --
                # except "SOP Missing" from a single-SOP-scoped run (the normal case
                # via the per-SOP "Check Compliance" button). "SOP Missing" there
                # means only that THIS document doesn't cover the topic -- exactly
                # what the Compliance Report itself labels "Not Applicable to This
                # SOP, not a finding". Treating it as an open, actionable gap
                # contradicted that and flooded Gaps & Redlines with "Major" items
                # for topics that are legitimately outside a given SOP's scope (e.g.
                # a visual-inspection SOP correctly not addressing HVAC design).
                # A genuine "no SOP anywhere at this site covers this topic"
                # documentation gap only makes sense from a full-site run, which
                # still creates/keeps the gap below.
                existing_gap = conn.execute("SELECT id FROM gaps WHERE rtm_entry_id=?", (entry_id,)).fetchone()
                if assessment["coverage_status"] == "SOP Missing" and not is_full_site_run:
                    if existing_gap:
                        conn.execute("UPDATE gaps SET status='Closed', updated_at=? WHERE id=?", (db.now(), existing_gap["id"]))
                elif assessment["coverage_status"] != "Covered":
                    # "SOP Missing" from a full-site run is a documentation-completeness
                    # gap (no relevant SOP uploaded anywhere at this site) -- cap it at
                    # Major so it doesn't compete for attention with Critical gaps found
                    # by actually reading an existing, in-scope SOP.
                    risk = "Major" if assessment["coverage_status"] == "SOP Missing" else _default_risk(req_d)
                    desc = f"{assessment['coverage_status']} for {req_d['source']} {req_d['clause']} at {site['code']}: {assessment.get('rationale','')}"
                    if not existing_gap:
                        conn.execute(
                            "INSERT INTO gaps (rtm_entry_id, site_id, requirement_id, description, risk_level, status, created_at, updated_at) VALUES (?,?,?,?,?, 'Open', ?, ?)",
                            (entry_id, site_id, req_d["id"], desc, risk, db.now(), db.now()),
                        )
                    else:
                        conn.execute("UPDATE gaps SET description=?, updated_at=? WHERE id=?", (desc, db.now(), existing_gap["id"]))

                results.append({
                    "requirement_id": req_d["id"], "req_code": req_d["req_code"],
                    "source": req_d["source"], "clause": req_d["clause"], "requirement_text": req_d["requirement_text"],
                    **assessment,
                })
                conn.commit()  # commit progress after each requirement so a later failure doesn't lose earlier work
            except Exception as e:
                # A single bad AI response or unexpected error must not take down the
                # whole 41-requirement run -- record it, skip it, and keep going so
                # the rest of the RTM still gets populated.
                conn.rollback()
                failed.append({"requirement_id": req_d["id"], "req_code": req_d["req_code"], "error": str(e)})
                db.log_error(
                    "rtm_requirement_assessment",
                    f"{type(e).__name__}: {e}",
                    traceback_text=traceback.format_exc(),
                    endpoint="run_rtm_mapping (background job)",
                    context={"site_id": site_id, "requirement_id": req_d["id"], "req_code": req_d["req_code"]},
                )
            job.update(done=job["done"] + 1)

        db.log_audit(actor, "run_rtm_mapping", "site", site_id, {"requirements_assessed": len(results), "failed": len(failed)})

        # General writing-quality/completeness review -- independent of the
        # regulatory RTM assessment above. Only meaningful for a single,
        # specific SOP (the normal case via the per-SOP "Check Compliance"
        # button); skipped for a full-site or multi-SOP run where there's no
        # one document's prose to review.
        general_issues, general_ai_mock, general_offline_note = [], False, None
        if len(sops) == 1:
            try:
                general_result = ai_service.review_sop_quality(sops[0]["title"], sops[0]["full_text"])
                general_issues = general_result["issues"]
                general_ai_mock = general_result["ai_mock"]
                general_offline_note = general_result.get("offline_note")
            except Exception as e:
                db.log_error("general_sop_quality_review", f"{type(e).__name__}: {e}", traceback_text=traceback.format_exc(), context={"site_id": site_id})

        # Generate the downloadable Compliance Report (+ a short Summary
        # Report) and log them so they show up in the Reports tab -- the
        # on-screen results only last as long as this job/page does, the
        # reports persist (until the next Render redeploy wipes local files
        # if no persistent disk is attached, same limitation as everything
        # else generated here).
        report_id = None
        try:
            report_sop = sops[0] if len(sops) == 1 else {"sop_number": "Multiple SOPs", "title": f"{len(sops)} SOPs checked", "sop_category": ""}
            initiated_at = db.now()
            stored_name = f"compliance_report_site{site_id}_{secrets.token_hex(4)}.docx"
            report_path = os.path.join(GEN_DIR, stored_name)
            report_service.generate_compliance_report(
                report_path, report_sop, dict(site), results,
                ai_mock_any=any(r.get("ai_mock") for r in results),
                general_issues=general_issues, general_ai_mock=general_ai_mock, general_offline_note=general_offline_note,
                initiated_by=actor, initiated_at=initiated_at,
            )
            summary_name = f"compliance_summary_site{site_id}_{secrets.token_hex(4)}.docx"
            summary_path = os.path.join(GEN_DIR, summary_name)
            report_service.generate_compliance_summary_report(
                summary_path, report_sop, dict(site), results,
                general_issues=general_issues, initiated_by=actor, initiated_at=initiated_at,
            )
            title = f"Compliance Report — {report_sop['sop_number']} — {site['code']}"
            cur = conn.execute(
                "INSERT INTO reports (report_type, site_id, sop_id, title, filepath, summary_filepath, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                ("compliance_check", site_id, sops[0]["sop_id"] if len(sops) == 1 else None, title, report_path, summary_path, actor, initiated_at),
            )
            conn.commit()
            report_id = cur.lastrowid
        except Exception as e:
            db.log_error("compliance_report_generation", f"{type(e).__name__}: {e}", traceback_text=traceback.format_exc(), context={"site_id": site_id})

        job.update(status="done", results=results, failed=failed, report_id=report_id, general_issues=general_issues)
    except Exception as e:
        job.update(status="error", error=str(e))
        db.log_error(
            "rtm_job",
            f"{type(e).__name__}: {e}",
            traceback_text=traceback.format_exc(),
            endpoint="run_rtm_mapping (background job)",
            context={"site_id": site_id},
        )
    finally:
        conn.close()


@app.route("/api/rtm/run-mapping", methods=["POST"])
@require_auth(roles=["admin", "analyst"])
def run_rtm_mapping():
    """Kicks off AI-assisted coverage assessment for every requirement against
    a site's SOP set, in a background thread, and returns immediately with a
    job_id to poll -- see GET /api/rtm/run-mapping/<job_id>.

    This used to run synchronously inside the request and return the full
    result set in one response. With a live Claude API key, a 41-requirement
    run is 41 sequential AI calls -- easily 1-2+ minutes wall-clock -- and
    Render's routing layer (and many browsers/proxies) will time out and kill
    the connection well before that, which surfaced as "Request failed" in the
    UI even though the run might have still been progressing server-side.
    Returning immediately and polling avoids that entirely, regardless of how
    long the underlying analysis takes.

    Accepts an optional "sop_ids" list to restrict the run to specific SOPs at
    the site rather than every SOP uploaded there. If omitted or empty, every
    current SOP at the site is considered.
    """
    data = request.get_json(force=True)
    site_id = data["site_id"]
    requested_sop_ids = data.get("sop_ids") or []
    actor = actor_label()  # must capture here -- actor_label() reads Flask's request-local `g`, unavailable in the worker thread

    job_id = secrets.token_hex(8)
    RTM_JOBS[job_id] = {"status": "running", "site_id": site_id, "total": 0, "done": 0, "results": [], "failed": [], "error": None, "report_id": None, "started_at": db.now()}
    thread = threading.Thread(target=_run_rtm_job, args=(job_id, site_id, requested_sop_ids, actor), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id, "status": "running"}), 202


@app.route("/api/rtm/run-mapping/<job_id>", methods=["GET"])
@require_auth()
def rtm_job_status(job_id):
    job = RTM_JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job_not_found"}), 404
    response = {"status": job["status"], "total": job["total"], "done": job["done"]}
    if job["status"] == "done":
        response["results"] = job["results"]
        response["report_id"] = job.get("report_id")
        response["general_issues_count"] = len(job.get("general_issues") or [])
        if job["failed"]:
            response["failed"] = job["failed"]
            response["warning"] = f"{len(job['failed'])} of {job['total']} requirement(s) could not be assessed and were skipped -- see 'failed' for details. The rest completed normally."
    elif job["status"] == "error":
        response["error"] = job["error"]
    return jsonify(response)


@app.route("/api/rtm/<int:entry_id>/confirm", methods=["PUT"])
@require_auth(roles=["admin", "analyst"])
def confirm_rtm_entry(entry_id):
    data = request.get_json(force=True)
    conn = db.get_db()
    conn.execute(
        "UPDATE rtm_entries SET coverage_status=?, rationale=?, ai_proposed=0, confirmed_by=?, confirmed_at=?, updated_at=? WHERE id=?",
        (data["coverage_status"], data.get("rationale", ""), actor_label(), db.now(), db.now(), entry_id),
    )
    conn.commit()
    conn.close()
    db.log_audit(actor_label(), "confirm_override", "rtm_entry", entry_id, data)
    return jsonify({"ok": True})


def _default_risk(requirement):
    critical_keywords = ["sterili", "aseptic", "media fill", "endotoxin", "pyrogen", "contamination"]
    text = (requirement["requirement_text"] + " " + requirement["process_area"]).lower()
    if any(k in text for k in critical_keywords):
        return "Critical"
    return "Major"


# ---------------------------------------------------------------------------
# Site comparison
# ---------------------------------------------------------------------------
@app.route("/api/comparison/run", methods=["POST"])
@require_auth(roles=["admin", "analyst"])
def run_comparison():
    """User explicitly picks which specific SOPs to compare (across any plants),
    rather than the system auto-pulling every SOP that happens to share a
    category tag. Requires at least 2 SOPs; they don't have to share a category
    -- the AI reads the actual content and reports concrete differences (or, if
    the documents genuinely aren't comparable, says so in the findings)."""
    data = request.get_json(force=True)
    sop_ids = data.get("sop_ids") or []
    if len(sop_ids) < 2:
        return jsonify({"error": "select_at_least_two_sops"}), 400

    conn = db.get_db()
    placeholders = ",".join("?" for _ in sop_ids)
    rows = conn.execute(f"""
        SELECT sop.id as sop_id, sites.code as site_code, sites.name as site_name,
               sop.sop_number, sop.title, sop.sop_category
        FROM sops sop
        JOIN sites ON sites.id = sop.site_id
        WHERE sop.id IN ({placeholders})
    """, sop_ids).fetchall()
    if len(rows) < 2:
        conn.close()
        return jsonify({"error": "one_or_more_selected_sops_not_found"}), 400

    site_sops = []
    for r in rows:
        site_sops.append({
            "site_code": r["site_code"], "site_name": r["site_name"],
            "sop_number": r["sop_number"], "title": r["title"],
            "text": db.full_sop_text(conn, r["sop_id"]),
        })
    categories = sorted({r["sop_category"] for r in rows if r["sop_category"]})
    label = categories[0] if len(categories) == 1 else (", ".join(categories) if categories else "Selected SOPs")

    result = ai_service.compare_sops_across_sites(label, site_sops)
    for finding in result["findings"]:
        conn.execute(
            "INSERT INTO comparison_findings (sop_category, process_step, site_values_json, classification, note, ai_mock, created_at) VALUES (?,?,?,?,?,?,?)",
            (label, finding.get("process_step", ""), __import__("json").dumps(finding.get("site_values", {})),
             finding.get("classification", "Best-Practice Divergence"), finding.get("note", ""), int(bool(result["ai_mock"])), db.now()),
        )

    report_id = None
    try:
        sops_compared = [{"site_code": r["site_code"], "site_name": r["site_name"], "sop_number": r["sop_number"], "title": r["title"]} for r in rows]
        stored_name = f"comparison_report_{secrets.token_hex(4)}.docx"
        report_path = os.path.join(GEN_DIR, stored_name)
        report_service.generate_comparison_report(report_path, sops_compared, result["findings"], ai_mock=result.get("ai_mock", False))
        title = f"Comparison Report — {', '.join(r['sop_number'] for r in rows)}"
        cur = conn.execute(
            "INSERT INTO reports (report_type, site_id, sop_id, title, filepath, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
            ("comparison", None, None, title, report_path, actor_label(), db.now()),
        )
        report_id = cur.lastrowid
    except Exception as e:
        db.log_error("comparison_report_generation", f"{type(e).__name__}: {e}", traceback_text=traceback.format_exc(), context={"sop_ids": sop_ids})

    conn.commit()
    conn.close()
    db.log_audit(actor_label(), "run_comparison", "sop_ids", None, {"sop_ids": sop_ids, "findings": len(result["findings"])})
    result["report_id"] = report_id
    return jsonify(result)


@app.route("/api/comparison", methods=["GET"])
@require_auth()
def list_comparison():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM comparison_findings ORDER BY id DESC").fetchall()
    conn.close()
    return jsonify(db.rows_to_list(rows))


# ---------------------------------------------------------------------------
# Gaps
# ---------------------------------------------------------------------------
@app.route("/api/gaps", methods=["GET"])
@require_auth()
def list_gaps():
    site_id = request.args.get("site_id")
    conn = db.get_db()
    q = """
        SELECT g.*, s.code as site_code, s.name as site_name, req.req_code, req.source, req.clause, req.requirement_text, req.sop_category,
               sop.id as gap_sop_id, sop.sop_number as gap_sop_number, sop.title as gap_sop_title
        FROM gaps g
        JOIN sites s ON s.id = g.site_id
        LEFT JOIN requirements req ON req.id = g.requirement_id
        LEFT JOIN rtm_entries rtm ON rtm.id = g.rtm_entry_id
        LEFT JOIN sops sop ON sop.id = rtm.sop_id
    """
    params = []
    if site_id:
        q += " WHERE g.site_id = ?"
        params.append(site_id)
    q += " ORDER BY sop.sop_number IS NULL, sop.sop_number, CASE g.risk_level WHEN 'Critical' THEN 0 WHEN 'Major' THEN 1 ELSE 2 END, g.id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify(db.rows_to_list(rows))


@app.route("/api/gaps/<int:gap_id>", methods=["PUT"])
@require_auth()
def update_gap(gap_id):
    data = request.get_json(force=True)
    fields, values = [], []
    for key in ["owner", "target_date", "status", "risk_level"]:
        if key in data:
            fields.append(f"{key}=?")
            values.append(data[key])
    if fields:
        values.append(db.now())
        values.append(gap_id)
        conn = db.get_db()
        conn.execute(f"UPDATE gaps SET {', '.join(fields)}, updated_at=? WHERE id=?", values)
        conn.commit()
        conn.close()
        db.log_audit(actor_label(), "update", "gap", gap_id, data)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# AI-assisted tracked-change redlining
# ---------------------------------------------------------------------------
@app.route("/api/gaps/<int:gap_id>/generate-redline", methods=["POST"])
@require_auth(roles=["admin", "analyst", "site_owner"])
def generate_redline(gap_id):
    conn = db.get_db()
    gap = conn.execute("SELECT * FROM gaps WHERE id=?", (gap_id,)).fetchone()
    if not gap:
        conn.close()
        return jsonify({"error": "gap_not_found"}), 404
    requirement = conn.execute("SELECT * FROM requirements WHERE id=?", (gap["requirement_id"],)).fetchone()
    site = conn.execute("SELECT * FROM sites WHERE id=?", (gap["site_id"],)).fetchone()

    data = request.get_json(silent=True) or {}
    sop_id = data.get("sop_id")
    if not sop_id:
        rtm = conn.execute("SELECT sop_id FROM rtm_entries WHERE id=?", (gap["rtm_entry_id"],)).fetchone() if gap["rtm_entry_id"] else None
        sop_id = rtm["sop_id"] if rtm and rtm["sop_id"] else None
    if not sop_id:
        candidate = conn.execute("SELECT id FROM sops WHERE site_id=? AND sop_category=? LIMIT 1", (gap["site_id"], requirement["sop_category"])).fetchone()
        sop_id = candidate["id"] if candidate else None
    if not sop_id:
        # Last resort: if this site only has one SOP uploaded at all, there's no
        # real ambiguity about which document a gap belongs to, even if the
        # rtm_entries.sop_id wasn't recorded (e.g. an older run, or the model
        # didn't echo it back) and the SOP's category text doesn't exactly match
        # the requirement's category label.
        site_sops = conn.execute("SELECT id FROM sops WHERE site_id=?", (gap["site_id"],)).fetchall()
        if len(site_sops) == 1:
            sop_id = site_sops[0]["id"]
    if not sop_id:
        conn.close()
        return jsonify({
            "error": "no_target_sop_identified",
            "hint": "pass sop_id explicitly in the request body -- multiple SOPs exist for this site and none could be confidently matched to this gap",
        }), 400

    sop = conn.execute("SELECT * FROM sops WHERE id=?", (sop_id,)).fetchone()
    version = conn.execute("SELECT * FROM sop_versions WHERE sop_id=? AND is_current=1", (sop_id,)).fetchone()
    if not version:
        conn.close()
        return jsonify({"error": "sop_has_no_current_version"}), 400
    if not version["filepath"] or not os.path.exists(version["filepath"]):
        conn.close()
        return jsonify({
            "error": "original_sop_file_missing",
            "message": (
                "The original uploaded file for this SOP no longer exists on the server "
                "(Render's free tier wipes locally stored files on every redeploy). A redline "
                "can't be generated without the original document -- re-upload this SOP, then try again."
            ),
        }), 400

    requirement_d = db.row_to_dict(requirement)
    # Give the drafting model the whole SOP package (main doc + annexures/formats),
    # not just the main file, so it doesn't duplicate content that already lives
    # in an attached annexure or format.
    full_text = db.full_sop_text(conn, sop_id) or version["extracted_text"] or ""
    draft = ai_service.draft_redline(requirement_d, gap["description"], full_text, sop["title"])

    new_version_label = _bump_version(version["version_label"])
    stored_name = f"redline_sop{sop_id}_gap{gap_id}_{secrets.token_hex(4)}.docx"
    output_path = os.path.join(GEN_DIR, stored_name)
    docx_service.generate_tracked_redline(
        version["filepath"], output_path, draft["heading"], draft["paragraphs"], requirement_d,
        new_version_label=new_version_label, gap_description=gap["description"],
    )

    summary_name = f"summary_sop{sop_id}_gap{gap_id}_{secrets.token_hex(4)}.docx"
    summary_path = os.path.join(GEN_DIR, summary_name)
    summary_service.generate_change_summary(
        summary_path, sop_title=sop["title"], sop_number=sop["sop_number"], site_name=site["name"],
        prev_version=version["version_label"], new_version=new_version_label,
        revision_date=datetime.date.today().isoformat(), prepared_by=actor_label(),
        requirement=requirement_d, gap_description=gap["description"], change_heading=draft["heading"],
        change_paragraphs=draft["paragraphs"], ai_mock=draft.get("ai_mock", False),
    )

    cur = conn.execute(
        """INSERT INTO sop_revisions (gap_id, sop_id, base_version_id, new_version_label, draft_filepath,
           summary_filepath, ai_mock, status, created_by, created_at)
           VALUES (?,?,?,?,?,?,?, 'Draft', ?, ?)""",
        (gap_id, sop_id, version["id"], new_version_label, output_path, summary_path,
         int(bool(draft.get("ai_mock"))), actor_label(), db.now()),
    )
    revision_id = cur.lastrowid
    conn.execute("UPDATE gaps SET status='In Progress', updated_at=? WHERE id=?", (db.now(), gap_id))
    conn.commit()
    conn.close()

    db.log_audit(actor_label(), "generate_redline", "sop_revision", revision_id, {"gap_id": gap_id, "ai_mock": draft.get("ai_mock")})
    return jsonify({"revision_id": revision_id, "new_version_label": new_version_label, "ai_mock": draft.get("ai_mock", False), "draft": draft})


def _bump_version(label):
    m = re.match(r"v?(\d+)\.(\d+)", label or "v1.0")
    if m:
        major, minor = int(m.group(1)), int(m.group(2))
        return f"v{major}.{minor + 1}"
    return "v1.1"


@app.route("/api/revisions", methods=["GET"])
@require_auth()
def list_revisions():
    conn = db.get_db()
    rows = conn.execute("""
        SELECT rev.*, sop.sop_number, sop.title as sop_title, s.code as site_code
        FROM sop_revisions rev
        JOIN sops sop ON sop.id = rev.sop_id
        JOIN sites s ON s.id = sop.site_id
        ORDER BY rev.id DESC
    """).fetchall()
    conn.close()
    return jsonify(db.rows_to_list(rows))


@app.route("/api/revisions/<int:revision_id>/download/<kind>", methods=["GET"])
@require_auth()
def download_revision_file(revision_id, kind):
    conn = db.get_db()
    rev = conn.execute("SELECT * FROM sop_revisions WHERE id=?", (revision_id,)).fetchone()
    conn.close()
    if not rev:
        return jsonify({"error": "not_found"}), 404
    path = rev["draft_filepath"] if kind == "redline" else rev["summary_filepath"] if kind == "summary" else None
    if not path or not os.path.exists(path):
        return jsonify({
            "error": "file_not_found",
            "message": (
                "This file no longer exists on the server. Render's free tier wipes locally "
                "stored files (uploads and generated documents) every time the app redeploys, "
                "which just happened. Re-run Generate Redline for this gap to recreate it -- if "
                "that also fails, the original SOP file was wiped too and needs to be re-uploaded first."
            ),
        }), 404
    fname = f"{kind}_{rev['id']}.docx"
    return send_file(path, as_attachment=True, download_name=fname)


# ---------------------------------------------------------------------------
# Reports: downloadable Compliance Reports and Comparison Reports
# ---------------------------------------------------------------------------
@app.route("/api/reports", methods=["GET"])
@require_auth()
def list_reports():
    site_id = request.args.get("site_id")
    conn = db.get_db()
    q = """
        SELECT r.*, s.code as site_code, s.name as site_name, sop.sop_number
        FROM reports r
        LEFT JOIN sites s ON s.id = r.site_id
        LEFT JOIN sops sop ON sop.id = r.sop_id
    """
    params = []
    if site_id:
        q += " WHERE r.site_id = ?"
        params.append(site_id)
    q += " ORDER BY r.id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return jsonify(db.rows_to_list(rows))


@app.route("/api/reports/<int:report_id>/download", methods=["GET"])
@require_auth()
def download_report(report_id):
    kind = request.args.get("kind", "detailed")  # "detailed" | "summary"
    conn = db.get_db()
    rep = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
    conn.close()
    if not rep:
        return jsonify({"error": "not_found"}), 404
    filepath = rep["summary_filepath"] if kind == "summary" and rep["summary_filepath"] else rep["filepath"]
    if not filepath or not os.path.exists(filepath):
        return jsonify({
            "error": "file_not_found",
            "message": (
                "This report file no longer exists on the server. If no persistent disk is attached, "
                "Render wipes locally stored files on every redeploy/restart. Re-run the compliance "
                "check or comparison to regenerate it."
            ),
        }), 404
    safe_title = re.sub(r"[^A-Za-z0-9_.-]", "_", rep["title"])
    suffix = "_Summary" if kind == "summary" else ""
    return send_file(filepath, as_attachment=True, download_name=f"{safe_title}{suffix}.docx")


@app.route("/api/revisions/<int:revision_id>/decision", methods=["POST"])
@require_auth(roles=["qa_reviewer", "quality_lead", "admin"])
def decide_revision(revision_id):
    data = request.get_json(force=True)
    decision = data.get("decision")  # 'Approved' | 'Rejected'
    signature_name = data.get("signature_name")
    notes = data.get("notes", "")
    if decision not in ("Approved", "Rejected") or not signature_name:
        return jsonify({"error": "decision and signature_name are required"}), 400

    conn = db.get_db()
    rev = conn.execute("SELECT * FROM sop_revisions WHERE id=?", (revision_id,)).fetchone()
    if not rev:
        conn.close()
        return jsonify({"error": "not_found"}), 404

    conn.execute(
        "UPDATE sop_revisions SET status=?, decided_by=?, decided_at=?, decision_notes=? WHERE id=?",
        (decision, f"{signature_name} ({actor_label()})", db.now(), notes, revision_id),
    )
    if decision == "Approved":
        conn.execute(
            "INSERT INTO sop_versions (sop_id, version_label, filename, filepath, extracted_text, is_current, uploaded_by, uploaded_at) VALUES (?,?,?,?,?,1,?,?)",
            (rev["sop_id"], rev["new_version_label"], os.path.basename(rev["draft_filepath"]), rev["draft_filepath"],
             docx_service.extract_text(rev["draft_filepath"]), actor_label(), db.now()),
        )
        conn.execute("UPDATE sop_versions SET is_current=0 WHERE sop_id=? AND id!=(SELECT MAX(id) FROM sop_versions WHERE sop_id=?)", (rev["sop_id"], rev["sop_id"]))
        gap = conn.execute("SELECT * FROM gaps WHERE id=?", (rev["gap_id"],)).fetchone()
        conn.execute("UPDATE gaps SET status='Closed', updated_at=? WHERE id=?", (db.now(), rev["gap_id"]))
        if gap and gap["rtm_entry_id"]:
            conn.execute(
                "UPDATE rtm_entries SET coverage_status='Covered', sop_id=?, confirmed_by=?, confirmed_at=?, updated_at=? WHERE id=?",
                (rev["sop_id"], f"{signature_name} (e-signature)", db.now(), db.now(), gap["rtm_entry_id"]),
            )
    conn.commit()
    conn.close()
    db.log_audit(f"{signature_name} ({actor_label()})", f"e-signature:{decision}", "sop_revision", revision_id, {"notes": notes})
    return jsonify({"ok": True, "status": decision})


# ---------------------------------------------------------------------------
# Dashboard & audit
# ---------------------------------------------------------------------------
@app.route("/api/dashboard/stats", methods=["GET"])
@require_auth()
def dashboard_stats():
    conn = db.get_db()
    sites = db.rows_to_list(conn.execute("SELECT * FROM sites ORDER BY code").fetchall())
    total_reqs = conn.execute("SELECT COUNT(*) c FROM requirements").fetchone()["c"]
    per_site = []
    for site in sites:
        counts = {"Covered": 0, "Partially Covered": 0, "Not Covered": 0, "SOP Missing": 0, "Not Assessed": 0}
        rows = conn.execute("SELECT coverage_status, COUNT(*) c FROM rtm_entries WHERE site_id=? GROUP BY coverage_status", (site["id"],)).fetchall()
        assessed = 0
        for r in rows:
            counts[r["coverage_status"]] = r["c"]
            assessed += r["c"]
        counts["Not Assessed"] = max(total_reqs - assessed, 0)
        open_gaps = conn.execute("SELECT risk_level, COUNT(*) c FROM gaps WHERE site_id=? AND status!='Closed' GROUP BY risk_level", (site["id"],)).fetchall()
        gap_counts = {"Critical": 0, "Major": 0, "Minor": 0}
        for g in open_gaps:
            gap_counts[g["risk_level"]] = g["c"]
        per_site.append({"site": site, "coverage": counts, "total_requirements": total_reqs, "open_gaps": gap_counts})
    conn.close()
    return jsonify({"total_requirements": total_reqs, "sites": per_site})


@app.route("/api/audit", methods=["GET"])
@require_auth(roles=["admin", "quality_lead", "auditor"])
def list_audit():
    conn = db.get_db()
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 500").fetchall()
    conn.close()
    return jsonify(db.rows_to_list(rows))


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "ai_live": bool(ai_service.ANTHROPIC_API_KEY)})


if __name__ == "__main__":
    db.init_db()
    port = int(os.environ.get("PORT", 5057))
    app.run(host="0.0.0.0", port=port, debug=True)
