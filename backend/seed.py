"""Seeds the database with: the regulatory requirement library (same 41
clauses used in RTM_Sterile_Injectable_Template.xlsx), demo sites, and demo
user accounts covering every role. Safe to re-run (idempotent on requirements
via req_code UNIQUE constraint; sites/users skipped if already present).
"""
import db
from app import hash_password

REQUIREMENTS = [
("FDA 21 CFR 211","§211.42","Facility Design","Buildings must be of suitable size, construction, and location for aseptic operations, with separate/defined areas to prevent contamination and mix-ups.","Facility Qualification & Room Classification"),
("FDA 21 CFR 211","§211.46","HVAC","Adequate ventilation with air filtration (HEPA where appropriate) and pressure differentials to prevent contamination in sterile areas.","HVAC Qualification & Monitoring"),
("FDA 21 CFR 211","§211.63/.65","Equipment","Equipment used in manufacture must be of appropriate design, size, and suitably located; contact surfaces must not react with product.","Equipment Qualification & Maintenance"),
("FDA 21 CFR 211","§211.67","Cleaning","Written procedures for cleaning and sanitizing equipment, with schedules, methods, and records.","Equipment Cleaning & Sanitization"),
("FDA 21 CFR 211","§211.94/.113","Sterilization","Written procedures to prevent microbiological contamination of drug products purporting to be sterile, including validation of sterilization processes.","Sterilization Process Validation"),
("FDA 21 CFR 211","§211.100/.101","Production & Process Controls","Written production and process control procedures, including in-process controls and specifications for aseptic filling operations.","Aseptic Filling / Process Control"),
("FDA 21 CFR 211","§211.113(b)","Aseptic Processing","Written procedures for aseptic processing, including validation of aseptic and sterilization processes (media fills).","Aseptic Process Simulation (Media Fill)"),
("FDA 21 CFR 211","§211.25","Personnel","Personnel must have education, training, and experience for their function; ongoing training in cGMP and job-specific aseptic technique.","Personnel Training & Gowning Qualification"),
("FDA 21 CFR 211","§211.28","Personnel Hygiene","Personnel practices for clean/sterile clothing, gowning procedures, and health/hygiene controls to prevent contamination.","Gowning & Personnel Hygiene"),
("FDA 21 CFR 211","§211.42(c)(10)","Environmental Monitoring","Establishment of a system for monitoring environmental conditions in aseptic processing areas.","Environmental Monitoring Program"),
("FDA 21 CFR 211","§211.84/.113","Component/Container Control","Testing and approval/rejection of components, containers, and closures; control of container-closure integrity.","Incoming Material & Component Control"),
("FDA 21 CFR 211","§211.160-167","Laboratory Controls","Establishment of scientifically sound laboratory controls, including sterility, endotoxin/pyrogen, and particulate testing.","QC Sterility & Endotoxin Testing"),
("FDA 21 CFR 211","§211.188/.192","Batch Records","Complete batch production and control records reviewed and approved prior to release; documented investigation of discrepancies.","Batch Record Review & Release"),
("FDA 21 CFR 211","§211.100(a)","Change Control","Written procedures for changes to specifications, methods, and procedures, drafted, reviewed, and approved by the quality control unit.","Change Control"),
("FDA 21 CFR 211","§211.192","Deviation/CAPA","Investigation of any unexplained discrepancy or failure of a batch to meet specifications; documented CAPA.","Deviation & CAPA Management"),
("FDA 21 CFR 211","§211.68","Water Systems","Systems for water used in manufacturing (e.g., WFI) must be validated and monitored for chemical and microbial quality.","Water System (WFI) Qualification & Monitoring"),
("EU GMP Annex 1 (2022)","§1-6","Contamination Control","Establishment and maintenance of a holistic Contamination Control Strategy (CCS) covering facility, personnel, equipment, and process risks.","Contamination Control Strategy"),
("EU GMP Annex 1 (2022)","§4","Premises – Grade A/B/C/D","Cleanroom classification, qualification, and routine environmental/particle monitoring per grade, with defined action/alert limits.","Room Classification & EM Program"),
("EU GMP Annex 1 (2022)","§4.16-4.20","Barrier Technology","Use and qualification of RABS/isolators where applicable, including leak testing and decontamination cycles.","RABS / Isolator Qualification"),
("EU GMP Annex 1 (2022)","§6","HVAC & Air Filtration","HVAC system design, qualification, and monitoring to maintain required air classification and pressure cascades.","HVAC Qualification & Monitoring"),
("EU GMP Annex 1 (2022)","§7","Personnel Gowning","Detailed gowning qualification (initial and periodic requalification) with defined disqualification/retraining triggers.","Gowning & Personnel Hygiene"),
("EU GMP Annex 1 (2022)","§8","Equipment","Design and qualification of equipment to minimize contamination risk, including CIP/SIP systems.","Equipment Qualification & CIP/SIP"),
("EU GMP Annex 1 (2022)","§9","Utilities (Water/Gas/Steam)","Qualification and routine monitoring of water, gases, and steam systems used in sterile manufacture.","Water/Gas/Steam System Qualification"),
("EU GMP Annex 1 (2022)","§10","Production & Specific Technologies","Process-specific controls for aseptic and terminally sterilized products, including bioburden control before sterilization.","Aseptic Filling / Terminal Sterilization Control"),
("EU GMP Annex 1 (2022)","§8.123-8.129 / §9","Sterilization","Validation and routine control of sterilization methods (moist heat, dry heat, filtration, irradiation).","Sterilization Process Validation"),
("EU GMP Annex 1 (2022)","§9.33-9.40","Filtration","Validation of sterilizing-grade filters, including integrity testing pre- and post-use.","Sterile Filtration & Filter Integrity Testing"),
("EU GMP Annex 1 (2022)","§13","Environmental & Process Monitoring","Environmental and process monitoring program including viable/non-viable particle counts, with trending and alert/action limits.","Environmental Monitoring Program"),
("EU GMP Annex 1 (2022)","§9.45-9.51","Aseptic Process Simulation","Media fill / process simulation requirements, frequency, and worst-case design.","Aseptic Process Simulation (Media Fill)"),
("EU GMP Annex 1 (2022)","§14","Quality Control","Sterility testing, container closure integrity testing, and finished product release testing.","QC Sterility & CCI Testing"),
("ICH Q7","§2-3","Quality Management / Personnel","Quality unit responsibilities, personnel qualification, and organizational structure for GMP compliance.","Quality System & Personnel Qualification"),
("ICH Q7","§5","Process Equipment","Equipment design, qualification, calibration, and preventive maintenance program.","Equipment Qualification & Maintenance"),
("ICH Q7","§6","Documentation & Records","Requirements for document control, batch production records, and record retention.","Document Control & Batch Records"),
("ICH Q7","§12","Validation","Process validation policy, including qualification of critical systems and revalidation criteria.","Validation Master Plan"),
("ICH Q9","Whole guideline","Quality Risk Management","Formal quality risk management process applied to facility design, process, and change control decisions.","Quality Risk Management Program"),
("ICH Q10","§3","Pharmaceutical Quality System","Process performance and product quality monitoring, CAPA system, change management, and management review.","Pharmaceutical Quality System / Management Review"),
("WHO GMP (TRS 1044, Annex 2)","§4","Premises","Design of premises for sterile production, including airlocks, pass-throughs, and material/personnel flow.","Facility Qualification & Room Classification"),
("WHO GMP (TRS 1044, Annex 2)","§5","Sanitation","Sanitation program for clean areas including disinfectant qualification and rotation.","Cleaning & Disinfection Program"),
("WHO GMP (TRS 1044, Annex 2)","§6","Processing","Aseptic and terminal sterilization processing controls, including bioburden monitoring prior to sterilization.","Aseptic Filling / Terminal Sterilization Control"),
("WHO GMP (TRS 1044, Annex 2)","§7","Sterilization","Sterilization method validation and routine monitoring (autoclave, dry heat, filtration, gas).","Sterilization Process Validation"),
("WHO GMP (TRS 1044, Annex 2)","§3","Personnel","Personnel qualification, gowning, and health monitoring for aseptic areas.","Gowning & Personnel Hygiene"),
("WHO GMP (TRS 1044, Annex 2)","§8","Quality Control","Finished product testing including sterility, pyrogen/endotoxin, and particulate matter testing.","QC Sterility & Endotoxin Testing"),
]

DEMO_SITES = [
    ("SITE-01", "Plant A (example)", "TBD", "Sterile Injectable", "Aseptic Fill-Finish", "US"),
    ("SITE-02", "Plant B (example)", "TBD", "Sterile Injectable", "Terminal Sterilization", "EU"),
]

DEMO_USERS = [
    ("admin", "System Administrator", "admin123", "admin", None),
    ("analyst", "Regulatory Analyst", "analyst123", "analyst", None),
    ("site1owner", "Site 1 SOP Owner", "site123", "site_owner", 1),
    ("qa1", "Site 1 QA Reviewer", "qa123", "qa_reviewer", 1),
    ("quality_lead", "Global Quality Lead", "lead123", "quality_lead", None),
    ("auditor", "Auditor (read-only)", "audit123", "auditor", None),
]


def run():
    db.init_db()
    conn = db.get_db()
    for i, (source, clause, area, text, category) in enumerate(REQUIREMENTS, start=1):
        req_code = f"REQ-{i:03d}"
        conn.execute(
            "INSERT OR IGNORE INTO requirements (req_code, source, clause, process_area, requirement_text, sop_category, created_at) VALUES (?,?,?,?,?,?,?)",
            (req_code, source, clause, area, text, category, db.now()),
        )
    for code, name, location, product_type, sterilization, markets in DEMO_SITES:
        conn.execute(
            "INSERT OR IGNORE INTO sites (code, name, location, product_type, sterilization_method, markets, notes, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (code, name, location, product_type, sterilization, markets, "Demo/example site — replace with real plant data.", db.now()),
        )
    for username, name, password, role, site_id in DEMO_USERS:
        conn.execute(
            "INSERT OR IGNORE INTO users (username, name, password_hash, role, site_id, created_at) VALUES (?,?,?,?,?,?)",
            (username, name, hash_password(password), role, site_id, db.now()),
        )
    conn.commit()
    conn.close()
    print(f"Seeded {len(REQUIREMENTS)} requirements, {len(DEMO_SITES)} demo sites, {len(DEMO_USERS)} demo users.")
    print("Demo logins (username / password):")
    for username, name, password, role, site_id in DEMO_USERS:
        print(f"  {username} / {password}   -> role={role}")


if __name__ == "__main__":
    run()
