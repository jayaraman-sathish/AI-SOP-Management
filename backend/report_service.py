"""Generates downloadable Word reports for the two things this app previously
only ever showed as live, on-screen tables: a per-SOP compliance check (RTM
run) and a site-to-site SOP comparison. Both land in the Reports tab so a
user can come back and download them again later instead of the result only
existing as long as the page stays open.
"""
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

NAVY = RGBColor(0x1F, 0x38, 0x64)
STATUS_COLORS = {
    "Covered": RGBColor(0x1E, 0x7B, 0x34),
    "Partially Covered": RGBColor(0x9A, 0x5B, 0x00),
    "Not Covered": RGBColor(0xC0, 0x00, 0x00),
    "SOP Missing": RGBColor(0x8B, 0x00, 0x00),
}


def _shade_cell(cell, hex_color):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hex_color)
    tcPr.append(shd)


def _set_cell_text(cell, text, bold=False, color=None, size=10):
    cell.text = ""
    p = cell.paragraphs[0]
    run = p.add_run(str(text))
    run.bold = bold
    run.font.size = Pt(size)
    if color:
        run.font.color.rgb = color


def _title_block(doc, title, subtitle):
    for section in doc.sections:
        section.page_width = Inches(8.5)
        section.page_height = Inches(11)
    t = doc.add_paragraph()
    r = t.add_run(title)
    r.bold = True
    r.font.size = Pt(22)
    r.font.color.rgb = NAVY
    s = doc.add_paragraph()
    sr = s.add_run(subtitle)
    sr.italic = True
    sr.font.size = Pt(9)
    sr.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    doc.add_paragraph()


def generate_compliance_report(output_path, sop, site, rtm_results, ai_mock_any=False):
    """
    sop: dict with sop_number, title, sop_category
    site: dict with code, name
    rtm_results: list of {req_code, source, clause, requirement_text, coverage_status, rationale, sop_id}
        (the same shape returned by the RTM background job's "results" list)
    """
    doc = Document()
    _title_block(
        doc,
        "SOP Compliance Report",
        f"{sop['sop_number']} — {sop['title']}  |  {site['code']} — {site['name']}",
    )

    if ai_mock_any:
        warn = doc.add_paragraph()
        wr = warn.add_run("⚠ One or more assessments below were generated in OFFLINE HEURISTIC MODE (no live AI connected) — treat those as placeholder pending a live-AI re-run.")
        wr.bold = True
        wr.font.size = Pt(9)
        wr.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
        doc.add_paragraph()

    counts = {}
    for r in rtm_results:
        counts[r["coverage_status"]] = counts.get(r["coverage_status"], 0) + 1
    total = len(rtm_results)
    checked = total - counts.get("SOP Missing", 0)

    summary = doc.add_paragraph()
    sr = summary.add_run(
        f"{total} regulatory requirements evaluated. {checked} were within this SOP's actual scope "
        f"(the rest are correctly not applicable -- this SOP wasn't relevant to their subject matter). "
        f"Of those in scope: {counts.get('Covered', 0)} Covered, {counts.get('Partially Covered', 0)} "
        f"Partially Covered, {counts.get('Not Covered', 0)} Not Covered."
    )
    sr.font.size = Pt(10)
    doc.add_paragraph()

    h = doc.add_paragraph()
    hr = h.add_run("Compliant Points")
    hr.bold = True
    hr.font.size = Pt(14)
    hr.font.color.rgb = NAVY
    covered = [r for r in rtm_results if r["coverage_status"] == "Covered"]
    _result_table(doc, covered, "No fully-covered requirements were found in scope for this SOP.")

    doc.add_paragraph()
    h2 = doc.add_paragraph()
    h2r = h2.add_run("Non-Compliant / Partially Compliant Points")
    h2r.bold = True
    h2r.font.size = Pt(14)
    h2r.font.color.rgb = NAVY
    non_compliant = [r for r in rtm_results if r["coverage_status"] in ("Partially Covered", "Not Covered")]
    _result_table(doc, non_compliant, "No gaps were found in requirements within this SOP's scope.")

    missing = [r for r in rtm_results if r["coverage_status"] == "SOP Missing"]
    if missing:
        doc.add_paragraph()
        h3 = doc.add_paragraph()
        h3r = h3.add_run("Not Applicable to This SOP")
        h3r.bold = True
        h3r.font.size = Pt(14)
        h3r.font.color.rgb = NAVY
        note = doc.add_paragraph()
        nr = note.add_run(
            "These requirements were checked but don't pertain to this SOP's subject matter -- listed here for "
            "completeness, not as findings against this document."
        )
        nr.italic = True
        nr.font.size = Pt(9)
        nr.font.color.rgb = RGBColor(0x80, 0x80, 0x80)
        _result_table(doc, missing, "", compact=True)

    doc.save(output_path)
    return output_path


def _result_table(doc, results, empty_note, compact=False):
    if not results:
        p = doc.add_paragraph(empty_note)
        p.runs[0].italic = True if p.runs else None
        return
    cols = ["Req Code", "Source / Clause", "Status", "Rationale"] if not compact else ["Req Code", "Source / Clause", "Requirement"]
    table = doc.add_table(rows=1 + len(results), cols=len(cols))
    table.style = "Table Grid"
    hdr = table.rows[0]
    for i, text in enumerate(cols):
        _set_cell_text(hdr.cells[i], text, bold=True, color=RGBColor(0xFF, 0xFF, 0xFF))
        _shade_cell(hdr.cells[i], "1F3864")
    for ri, r in enumerate(results, start=1):
        row = table.rows[ri]
        _set_cell_text(row.cells[0], r.get("req_code", ""), bold=True, size=9)
        _set_cell_text(row.cells[1], f"{r.get('source','')} {r.get('clause','')}".strip(), size=9)
        if not compact:
            status_color = STATUS_COLORS.get(r.get("coverage_status"), None)
            _set_cell_text(row.cells[2], r.get("coverage_status", ""), bold=True, color=status_color, size=9)
            _set_cell_text(row.cells[3], (r.get("rationale") or "")[:500], size=9)
        else:
            _set_cell_text(row.cells[2], (r.get("requirement_text") or "")[:200], size=9)


def generate_comparison_report(output_path, sops_compared, findings, ai_mock=False):
    """
    sops_compared: list of {site_code, site_name, sop_number, title}
    findings: list of {process_step, site_values (dict), classification, note}
    """
    doc = Document()
    scope = "; ".join(f"{s['site_code']} — {s['sop_number']} {s['title']}" for s in sops_compared)
    _title_block(doc, "SOP Site Comparison Report", scope)

    if ai_mock:
        warn = doc.add_paragraph()
        wr = warn.add_run("⚠ Generated in OFFLINE HEURISTIC MODE (no live AI connected) — treat all findings as placeholder pending a live-AI re-run.")
        wr.bold = True
        wr.font.size = Pt(9)
        wr.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
        doc.add_paragraph()

    summary = doc.add_paragraph()
    sr = summary.add_run(f"{len(findings)} concrete difference(s) identified across {len(sops_compared)} SOP(s).")
    sr.font.size = Pt(10)
    doc.add_paragraph()

    if not findings:
        doc.add_paragraph("No differences were identified -- the compared SOPs appear consistent on the points reviewed.")
    else:
        site_codes = sorted({s["site_code"] for s in sops_compared})
        cols = ["Process Step"] + site_codes + ["Classification", "Note"]
        table = doc.add_table(rows=1 + len(findings), cols=len(cols))
        table.style = "Table Grid"
        hdr = table.rows[0]
        for i, text in enumerate(cols):
            _set_cell_text(hdr.cells[i], text, bold=True, color=RGBColor(0xFF, 0xFF, 0xFF), size=9)
            _shade_cell(hdr.cells[i], "1F3864")
        for ri, f in enumerate(findings, start=1):
            row = table.rows[ri]
            _set_cell_text(row.cells[0], f.get("process_step", ""), bold=True, size=9)
            site_values = f.get("site_values", {}) or {}
            for ci, code in enumerate(site_codes, start=1):
                _set_cell_text(row.cells[ci], site_values.get(code, ""), size=9)
            _set_cell_text(row.cells[len(site_codes) + 1], f.get("classification", ""), size=9)
            _set_cell_text(row.cells[len(site_codes) + 2], f.get("note", ""), size=9)

    doc.save(output_path)
    return output_path
