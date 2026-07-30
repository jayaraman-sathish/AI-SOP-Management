"""AI Orchestration Service (prototype).

Isolated module per the production spec (Section 7): all AI calls go through
here so the model/provider can be swapped without touching route logic.

If ANTHROPIC_API_KEY is set in the environment, calls the real Claude API.
Otherwise, falls back to a deterministic heuristic so the full pipeline
(upload -> RTM mapping -> gap -> redline -> change summary) remains testable
end-to-end without a key. Every AI response includes an "ai_mock" flag so the
frontend and audit trail can clearly distinguish live AI output from the
offline fallback -- this is never hidden from the reviewer, consistent with
the explainability requirement in the spec (Section 6.3).
"""
import os
import json
import re
import requests

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")
API_URL = "https://api.anthropic.com/v1/messages"


class AIError(Exception):
    pass


def _call_claude(system_prompt, user_prompt, max_tokens=2000):
    """Low-level call to the Anthropic Messages API. Raises AIError on failure."""
    if not ANTHROPIC_API_KEY:
        raise AIError("no_api_key")
    try:
        resp = requests.post(
            API_URL,
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=60,
        )
        if resp.status_code != 200:
            raise AIError(f"http_{resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return text
    except requests.RequestException as e:
        raise AIError(str(e))


def _extract_json(text):
    """Pull the first JSON object/array out of a model response, tolerating markdown fences."""
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if not match:
        raise AIError("no_json_in_response")
    return json.loads(match.group(1))


def _extract_json_object(text):
    """Like _extract_json, but guarantees a dict back. A live model occasionally
    wraps its answer in a JSON array (e.g. `[{...}]`) or returns some other
    valid-but-wrong-shaped JSON despite the prompt asking for an object -- if we
    don't guard against that here, code further down that does `parsed["key"]`
    throws a raw TypeError that isn't an AIError and isn't caught, crashing the
    entire batch (this happened in practice during live RTM runs). Treat any
    non-dict result the same as a parse failure so it falls through to the
    deterministic offline heuristic for just that one item instead."""
    parsed = _extract_json(text)
    if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
        parsed = parsed[0]  # common model quirk: wrapping a single object in an array
    if not isinstance(parsed, dict):
        raise AIError(f"expected_json_object_got_{type(parsed).__name__}")
    return parsed


# ---------------------------------------------------------------------------
# Task 1: Requirement -> SOP coverage assessment (RTM mapping)
# ---------------------------------------------------------------------------
def assess_requirement_coverage(requirement, sop_excerpts):
    """
    requirement: dict with source, clause, process_area, requirement_text, sop_category
    sop_excerpts: list of {sop_id, sop_number, title, text}
    returns: dict {coverage_status, rationale, cited_text, sop_id, ai_mock}
    """
    if not sop_excerpts:
        return {
            "coverage_status": "SOP Missing",
            "rationale": "No SOP at this site is tagged to the process area/category this requirement falls under.",
            "cited_text": "",
            "sop_id": None,
            "ai_mock": True,
        }

    system_prompt = (
        "You are a pharmaceutical GMP compliance analyst reviewing sterile/injectable manufacturing SOPs "
        "against a specific regulatory requirement. You are grounding every judgement in the provided SOP "
        "text only -- never invent SOP content. Respond with strict JSON only, no prose outside the JSON."
    )
    excerpt_block = "\n\n".join(
        f"--- SOP [{e['sop_id']}] {e['sop_number']} - {e['title']} ---\n{e['text'][:4000]}" for e in sop_excerpts
    )
    user_prompt = f"""Regulatory requirement to assess:
Source: {requirement['source']}
Clause: {requirement['clause']}
Process area: {requirement['process_area']}
Requirement text: {requirement['requirement_text']}

Candidate SOPs at this site (only these may be cited):
{excerpt_block}

Assess whether the requirement is covered. Respond with JSON exactly in this shape:
{{
  "coverage_status": "Covered" | "Partially Covered" | "Not Covered",
  "sop_id": <the sop_id number that is most relevant, or null>,
  "cited_text": "<short exact quote from the SOP supporting your assessment, or empty string>",
  "rationale": "<2-3 sentences explaining the assessment in plain language, referencing what is present or missing>"
}}"""
    try:
        raw = _call_claude(system_prompt, user_prompt, max_tokens=800)
        parsed = _extract_json_object(raw)
        if "coverage_status" not in parsed:
            raise AIError("missing_coverage_status_in_response")
        parsed["ai_mock"] = False
        return parsed
    except AIError:
        # Deterministic offline fallback: naive keyword overlap heuristic.
        best = max(sop_excerpts, key=lambda e: _keyword_overlap(requirement["requirement_text"], e["text"]))
        overlap = _keyword_overlap(requirement["requirement_text"], best["text"])
        if overlap > 0.35:
            status = "Covered"
        elif overlap > 0.12:
            status = "Partially Covered"
        else:
            status = "Not Covered"
        return {
            "coverage_status": status,
            "sop_id": best["sop_id"],
            "cited_text": best["text"][:200].strip(),
            "rationale": (
                f"[Offline heuristic mode - no ANTHROPIC_API_KEY configured] Keyword overlap between the "
                f"requirement and '{best['title']}' is {overlap:.0%}. This is a placeholder assessment; "
                f"connect a live Claude API key for real semantic analysis."
            ),
            "ai_mock": True,
        }


def _keyword_overlap(a, b):
    stop = {"the", "and", "of", "to", "for", "a", "in", "or", "with", "is", "are", "be", "that", "shall", "must", "on"}
    wa = set(w.lower().strip(".,()") for w in a.split() if len(w) > 3 and w.lower() not in stop)
    wb = set(w.lower().strip(".,()") for w in b.split() if len(w) > 3 and w.lower() not in stop)
    if not wa:
        return 0.0
    return len(wa & wb) / len(wa)


# ---------------------------------------------------------------------------
# Task 2: Site-to-site comparison
# ---------------------------------------------------------------------------
def compare_sops_across_sites(sop_category, site_sops):
    """
    site_sops: list of {site_code, site_name, sop_number, title, text}
    returns: dict {findings: [...], ai_mock}
    """
    system_prompt = (
        "You are a pharmaceutical GMP compliance analyst comparing how different manufacturing sites "
        "execute the same SOP category. Identify concrete, checkable differences (parameters, frequencies, "
        "acceptance criteria, steps) -- not vague summaries. Respond with strict JSON only."
    )
    blocks = "\n\n".join(
        f"--- Site {s['site_code']} ({s['site_name']}) - {s['sop_number']} {s['title']} ---\n{s['text'][:3500]}"
        for s in site_sops
    )
    user_prompt = f"""SOP category being compared: {sop_category}

SOPs from each site:
{blocks}

Identify the key differences between sites for this SOP category. For each difference, classify it as one of:
"No Regulatory Impact", "Best-Practice Divergence", "Compliance-Relevant Divergence".

Respond with JSON exactly in this shape:
{{
  "findings": [
    {{
      "process_step": "<short label, e.g. 'Media fill frequency'>",
      "site_values": {{"<site_code>": "<what that site's SOP says>", ...}},
      "classification": "No Regulatory Impact" | "Best-Practice Divergence" | "Compliance-Relevant Divergence",
      "note": "<1-2 sentence explanation, cite the regulatory concern if compliance-relevant>"
    }}
  ]
}}"""
    try:
        raw = _call_claude(system_prompt, user_prompt, max_tokens=1500)
        parsed = _extract_json_object(raw)
        findings = parsed.get("findings", [])
        if not isinstance(findings, list):
            raise AIError("findings_not_a_list")
        return {"findings": findings, "ai_mock": False}
    except AIError:
        return {
            "findings": [
                {
                    "process_step": "Overall SOP structure",
                    "site_values": {s["site_code"]: f"{s['sop_number']} v.—" for s in site_sops},
                    "classification": "Best-Practice Divergence",
                    "note": (
                        "[Offline heuristic mode - no ANTHROPIC_API_KEY configured] Documents differ in length/"
                        "structure; connect a live Claude API key for a real parameter-level comparison."
                    ),
                }
            ],
            "ai_mock": True,
        }


# ---------------------------------------------------------------------------
# Task 3: Tracked-change SOP redline drafting
# ---------------------------------------------------------------------------
def draft_redline(requirement, gap_description, sop_text, sop_title):
    system_prompt = (
        "You are a pharmaceutical GMP writer drafting the minimum SOP text needed to close a specific "
        "regulatory compliance gap. Write in formal SOP procedural language. Do not rewrite the whole "
        "document -- propose only the new/amended text needed. Respond with strict JSON only."
    )
    user_prompt = f"""SOP being revised: {sop_title}
Current SOP text (excerpt, may be partial): {sop_text[:4000]}

Regulatory requirement driving this revision:
Source: {requirement['source']} | Clause: {requirement['clause']}
Requirement text: {requirement['requirement_text']}

Gap identified: {gap_description}

Draft the SOP procedural text that closes this gap. Respond with JSON exactly in this shape:
{{
  "heading": "<short section heading for the new/amended text, e.g. 'Media Fill Frequency and Acceptance Criteria'>",
  "paragraphs": ["<paragraph 1 of new SOP text>", "<paragraph 2 if needed>"],
  "rationale": "<1-2 sentences on why this text satisfies the requirement>"
}}"""
    try:
        raw = _call_claude(system_prompt, user_prompt, max_tokens=1200)
        parsed = _extract_json_object(raw)
        if "paragraphs" not in parsed:
            raise AIError("missing_paragraphs_in_response")
        parsed["ai_mock"] = False
        return parsed
    except AIError:
        return {
            "heading": f"Regulatory Compliance Update - {requirement['clause']}",
            "paragraphs": [
                f"[OFFLINE HEURISTIC MODE - NO ANTHROPIC_API_KEY CONFIGURED. This is placeholder text only "
                f"and must be replaced with a real drafted procedure before use.]",
                f"This section addresses {requirement['source']} {requirement['clause']}: "
                f"{requirement['requirement_text'][:300]}",
                f"Gap being closed: {gap_description}",
            ],
            "rationale": "Placeholder text generated without live AI access.",
            "ai_mock": True,
        }
