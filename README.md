# SOP Compliance Intelligence Platform (Prototype)

A working prototype of the application specified in `SOP_Compliance_App_Spec.docx`:
multi-site SOP compliance for sterile / injectable pharmaceutical manufacturing, with
AI-assisted requirement-to-SOP mapping (RTM), site-to-site SOP comparison, gap
analysis, and **AI-assisted SOP redlining with Microsoft Word Track Changes enabled**,
plus a Change Summary generator and an electronic-signature-style review/approval
workflow.

Regulatory scope pre-loaded: **41 requirements** across US FDA 21 CFR 210/211, EU GMP
Annex 1 (2022), ICH Q7/Q9/Q10, and WHO GMP TRS 1044 Annex 2.

> **Status: prototype / MVP**, not yet validated for GxP production use. See
> "Known limitations & next steps" below and Section 9.3 (CSV/CSA validation) and
> Section 12 (phased roadmap) of `SOP_Compliance_App_Spec.docx` before any real use
> on live SOPs.

## Architecture

- **Backend**: Python (Flask) + SQLite (built into Python — no separate database
  server to install or manage). See `backend/`.
- **Frontend**: a single self-contained HTML/CSS/JS page (`frontend/index.html`) —
  no build step, no npm install required. Flask serves it directly.
- **AI**: calls the Anthropic Claude API directly over HTTPS (`backend/ai_service.py`)
  if `ANTHROPIC_API_KEY` is set. **Without a key, the app runs in a clearly-labeled
  offline heuristic mode** (keyword-overlap matching, placeholder redline text) so the
  full pipeline — upload → gap analysis → tracked-change redline → change summary →
  e-signature approval — is testable end-to-end with zero external dependencies.
  Every AI-derived result carries an `ai_mock: true/false` flag through the API, the
  UI, and the generated documents, so nobody can mistake placeholder output for a
  real assessment.
- **Track Changes**: `backend/docx_service.py` writes native OOXML `<w:ins>` markup
  directly into the uploaded `.docx`'s `word/document.xml`, so the generated redline
  opens in Microsoft Word with Track Changes already on — reviewers accept/reject
  each inserted block from Word's own Review pane. See "Known limitations" for the
  v1 scope of what gets redlined.

## Quick start (local)

Requires Python 3.10+. No `pip install` targets beyond the four packages below —
everything else (SQLite, JWT-style auth, etc.) uses the Python standard library or
these packages.

```bash
cd backend
pip install -r requirements.txt
python seed.py          # creates data/app.db, loads the 41 requirements, 2 demo
                         # sites, and 6 demo user accounts (see below)
python app.py            # starts on http://localhost:5057
```

Open **http://localhost:5057** in a browser. Log in with any of the seeded demo
accounts:

| Username | Password | Role |
|---|---|---|
| admin | admin123 | System Administrator |
| analyst | analyst123 | Regulatory/Quality Analyst |
| site1owner | site123 | Site SOP Owner (Site 1) |
| qa1 | qa123 | Site QA Reviewer (Site 1) |
| quality_lead | lead123 | Global Quality Lead |
| auditor | audit123 | Auditor (read-only) |

**Change or remove these before any real deployment.**

To enable live AI analysis instead of the offline heuristic fallback:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python app.py
```

## Typical walkthrough

1. Log in as `analyst`.
2. **SOP Repository** tab → Upload SOP — pick a real (or sample) `.docx`. Give it a
   **SOP Category** that matches one of the categories in the regulatory library
   (see the RTM tab, or `backend/seed.py`) so it gets auto-matched.
3. **RTM** tab → "Run AI Gap Analysis" — assesses all 41 requirements against that
   site's uploaded SOPs; anything not "Covered" becomes a gap.
4. **Gaps & Redlines** tab → "Generate Redline" on any open gap — produces a
   Track-Changes-enabled `.docx` and a Change Summary `.docx`.
5. **Review & Approval** tab → download and inspect the redline in Word (Review tab
   shows the tracked insertions); log in as `qa1` in another session/tab, type a name
   in the signature box, and Approve. The RTM entry flips to "Covered" and the SOP's
   current version advances.
6. **Audit Trail** tab (as `admin`, `quality_lead`, or `auditor`) — every action above
   is logged there.

## Deploying

### Render.com (recommended for a quick hosted demo)

A ready-to-use `render.yaml` is included at the repo root (Render's "Blueprint"
format):

1. Push this repo to GitHub (see below).
2. In Render: New → Blueprint → point at this repo. Render reads `render.yaml`
   automatically — one web service, a 1GB persistent disk for the SQLite DB and
   uploaded files, and an `ANTHROPIC_API_KEY` env var you set in the Render
   dashboard (marked `sync: false` in the blueprint so it isn't stored in git).
3. First deploy runs `python seed.py` (idempotent — safe on every redeploy) then
   starts `gunicorn`.

### Anywhere else

It's a standard Flask app — any host that runs Python (a VM, a container, PaaS) works.
Just make sure `DATA_DIR` points at a writable, **persistent** volume (the default is
a `data/` folder next to `backend/`, which is fine for a single persistent disk but
will NOT survive a redeploy on platforms with ephemeral filesystems unless you mount
a volume — set the `DATA_DIR` env var to that mount point).

## Known limitations & next steps (read before relying on this for real compliance work)

This is a **functional prototype**, built to prove out the workflow end-to-end — not
the hardened, validated production system described in `SOP_Compliance_App_Spec.docx`.
Specifically:

- **Redlining is additive, not surgical.** v1 inserts a new, clearly-headed tracked
  section with the AI-drafted procedure text rather than editing existing sentences
  in place (Word fragments text across many runs in ways that make blind
  find-and-replace unreliable — see `docx_service.py` docstring). In-place editing is
  a natural Phase 2 enhancement once tested against real site SOPs.
- **No vector database / RAG retrieval yet.** SOP-to-requirement matching in
  `ai_service.py` currently retrieves candidate SOPs by exact `sop_category` string
  match, then hands the full text to Claude for the actual semantic assessment. The
  production spec's Section 6.1 (embeddings + retrieval) would scale this better
  across large SOP libraries.
- **E-signatures are simulated**, not a validated 21 CFR Part 11 signature
  implementation — they capture a typed name, actor, and timestamp in the audit log,
  which is enough to prove the workflow but not enough for a real GxP release
  decision without further hardening (password re-entry at sign time, non-repudiation
  controls, etc. — see spec Section 9.2).
- **No formal CSV/CSA validation** has been performed — required per spec Section 9.3
  before this touches real, in-use SOPs.
- **Single SQLite file** is fine for the pilot/demo scale in the spec (a few sites,
  hundreds of SOPs) but should move to a managed Postgres instance for a real
  multi-site rollout (see spec Section 11).
- Demo user passwords are hardcoded in `seed.py` — replace with a real user
  provisioning process (ideally SSO, per spec Section 7.1) before any non-demo use.

## Repository layout

```
backend/
  app.py              Flask routes (auth, sites, SOPs, RTM, comparison, gaps,
                       redlining, revisions/approval, dashboard, audit)
  db.py                SQLite schema + connection helpers
  ai_service.py         AI orchestration (live Claude API call + offline fallback)
  docx_service.py       SOP text extraction + tracked-changes redline generation
  summary_service.py    Change Summary .docx generation
  seed.py               Regulatory library + demo sites/users seed data
  requirements.txt
frontend/
  index.html             Single-page app (vanilla JS, no build step)
data/                     SQLite DB + uploaded/generated files (gitignored except
                          placeholder dirs)
render.yaml               Render.com Blueprint for one-command hosted deploy
SOP_Compliance_App_Spec.docx        Full production requirements & architecture spec
SOP_Compliance_Framework.docx       Program methodology (comparison, gap analysis,
                                     redlining workflow)
SOP_Change_Summary_Template.docx    Manual template this app's generator is based on
RTM_Sterile_Injectable_Template.xlsx  Spreadsheet-based RTM (pre-app version)
```
