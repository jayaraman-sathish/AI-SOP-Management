"""SQLite database layer for the SOP Compliance Intelligence Platform (prototype).

Uses Python's built-in sqlite3 — no external DB server required, so an IT team
can run this anywhere Python runs. Schema mirrors the data model in the
production spec (SOP_Compliance_App_Spec.docx, Section 5).
"""
import sqlite3
import os
import json
import datetime

DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(os.path.dirname(__file__), "..", "data")
DB_PATH = os.path.join(DATA_DIR, "app.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now():
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,          -- admin | analyst | site_owner | qa_reviewer | quality_lead | auditor
    site_id INTEGER,             -- null = all-site scope (admin/analyst/quality_lead/auditor)
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    location TEXT,
    product_type TEXT,
    sterilization_method TEXT,
    markets TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requirements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    req_code TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL,
    clause TEXT NOT NULL,
    process_area TEXT NOT NULL,
    requirement_text TEXT NOT NULL,
    sop_category TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    sop_number TEXT NOT NULL,
    title TEXT NOT NULL,
    process_area TEXT,
    sop_category TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    UNIQUE(site_id, sop_number)
);

CREATE TABLE IF NOT EXISTS sop_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sop_id INTEGER NOT NULL REFERENCES sops(id),
    version_label TEXT NOT NULL,
    filename TEXT NOT NULL,
    filepath TEXT NOT NULL,
    extracted_text TEXT,
    is_current INTEGER NOT NULL DEFAULT 1,
    uploaded_by TEXT,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sop_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sop_version_id INTEGER NOT NULL REFERENCES sop_versions(id),
    doc_type TEXT NOT NULL DEFAULT 'Annexure',  -- Annexure | Format | Other
    filename TEXT NOT NULL,
    filepath TEXT NOT NULL,
    extracted_text TEXT,
    uploaded_by TEXT,
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rtm_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement_id INTEGER NOT NULL REFERENCES requirements(id),
    site_id INTEGER NOT NULL REFERENCES sites(id),
    sop_id INTEGER REFERENCES sops(id),
    sop_version_id INTEGER REFERENCES sop_versions(id),
    coverage_status TEXT NOT NULL DEFAULT 'Not Assessed',  -- Covered | Partially Covered | Not Covered | SOP Missing | Not Assessed
    rationale TEXT,
    cited_text TEXT,
    ai_proposed INTEGER NOT NULL DEFAULT 0,
    ai_mock INTEGER NOT NULL DEFAULT 0,
    confirmed_by TEXT,
    confirmed_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(requirement_id, site_id)
);

CREATE TABLE IF NOT EXISTS comparison_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sop_category TEXT NOT NULL,
    process_step TEXT NOT NULL,
    site_values_json TEXT NOT NULL,
    classification TEXT NOT NULL,   -- No Regulatory Impact | Best-Practice Divergence | Compliance-Relevant Divergence
    note TEXT,
    requirement_id INTEGER REFERENCES requirements(id),
    ai_mock INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rtm_entry_id INTEGER REFERENCES rtm_entries(id),
    comparison_finding_id INTEGER REFERENCES comparison_findings(id),
    site_id INTEGER NOT NULL REFERENCES sites(id),
    requirement_id INTEGER REFERENCES requirements(id),
    description TEXT NOT NULL,
    risk_level TEXT NOT NULL DEFAULT 'Minor',  -- Critical | Major | Minor
    owner TEXT,
    target_date TEXT,
    status TEXT NOT NULL DEFAULT 'Open',  -- Open | In Progress | Closed
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sop_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gap_id INTEGER NOT NULL REFERENCES gaps(id),
    sop_id INTEGER NOT NULL REFERENCES sops(id),
    base_version_id INTEGER NOT NULL REFERENCES sop_versions(id),
    new_version_label TEXT,
    draft_filepath TEXT,
    summary_filepath TEXT,
    ai_mock INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'Draft',  -- Draft | Submitted | QA Review | Approved | Rejected
    created_by TEXT,
    created_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    decision_notes TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    details_json TEXT,
    timestamp TEXT NOT NULL
);
"""


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def log_audit(actor, action, entity_type, entity_id=None, details=None):
    conn = get_db()
    conn.execute(
        "INSERT INTO audit_log (actor, action, entity_type, entity_id, details_json, timestamp) VALUES (?,?,?,?,?,?)",
        (actor, action, entity_type, entity_id, json.dumps(details or {}), now()),
    )
    conn.commit()
    conn.close()


def row_to_dict(row):
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows_to_list(rows):
    return [row_to_dict(r) for r in rows]


def full_sop_text(conn, sop_id):
    """Concatenated text of a SOP's current main document plus every attachment
    (annexures, formats) linked to that version -- used for AI analysis so gap
    assessment and redlining consider the whole SOP package, not just the main doc.
    """
    version = conn.execute("SELECT * FROM sop_versions WHERE sop_id=? AND is_current=1", (sop_id,)).fetchone()
    if not version:
        return ""
    parts = [f"=== MAIN SOP DOCUMENT: {version['filename']} ===\n{version['extracted_text'] or ''}"]
    attachments = conn.execute(
        "SELECT * FROM sop_attachments WHERE sop_version_id=? ORDER BY id", (version["id"],)
    ).fetchall()
    for a in attachments:
        parts.append(f"=== {a['doc_type'].upper()}: {a['filename']} ===\n{a['extracted_text'] or ''}")
    return "\n\n".join(parts)
