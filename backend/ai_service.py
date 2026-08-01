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
    """Pull the first JSON object/array out of a model response, tolerating markdown fences.

    Every call site catches AIError and falls back to a safe default (offline
    heuristic, or an honest "AI mock" placeholder) -- so any failure here must
    surface as AIError, never a raw exception. A live model response is
    sometimes truncated (hit max_tokens) or otherwise malformed JSON despite
    being asked for strict JSON only; without this try/except, json.loads
    raises json.JSONDecodeError directly, which no caller catches, so it was
    escaping all the way to Flask's global error handler as an unhandled
    "Something went wrong" error instead of degrading gracefully like every
    other AI failure mode does."""
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if not match:
        raise AIError("no_json_in_response")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as e:
        raise AIError(f"malformed_json_in_response ({e}) -- response may have been truncated")


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
    sop_excerpts: list of {sop_id, sop_number, title, text} -- every SOP currently
        checked for this site, NOT pre-filtered by category. Relevance is judged
        from actual content here, not from a category tag matching upstream --
        a mistagged or partially-relevant SOP can still get matched correctly,
        and a requirement with no genuinely relevant SOP gets an honest
        "SOP Missing" instead of a false "Not Covered" against an unrelated doc.
    returns: dict {coverage_status, rationale, cited_text, sop_id, ai_mock}
    """
    if not sop_excerpts:
        return {
            "coverage_status": "SOP Missing",
            "rationale": "No SOP has been uploaded for this site at all.",
            "cited_text": "",
            "sop_id": None,
            "ai_mock": True,
        }

    system_prompt = (
        "You are a pharmaceutical GMP compliance analyst reviewing sterile/injectable manufacturing SOPs "
        "against a specific regulatory requirement. You are grounding every judgement in the provided SOP "
        "text only -- never invent SOP content. The SOPs you're given are not pre-filtered for relevance -- "
        "some may have nothing to do with this requirement's subject matter, and you must judge that from "
        "their actual content, not assume relevance. Respond with strict JSON only, no prose outside the JSON."
    )
    excerpt_block = "\n\n".join(
        f"--- SOP [{e['sop_id']}] {e['sop_number']} - {e['title']} ---\n{e['text'][:4000]}" for e in sop_excerpts
    )
    user_prompt = f"""Regulatory requirement to assess:
Source: {requirement['source']}
Clause: {requirement['clause']}
Process area: {requirement['process_area']}
Requirement text: {requirement['requirement_text']}

Candidate SOPs at this site (only these may be cited -- judge relevance from their actual content, they are not pre-filtered):
{excerpt_block}

First decide: does ANY of these SOPs actually address this requirement's subject matter at all? If none of
them are topically relevant (e.g. a visual-inspection SOP being checked against a facility-design or
sterility-testing requirement), respond with coverage_status "SOP Missing" -- this means "no relevant SOP
exists to check", which is different from "Not Covered" (which means a relevant SOP exists but falls short).
Only use "Covered" / "Partially Covered" / "Not Covered" when at least one SOP is genuinely on-topic for this
requirement. Respond with JSON exactly in this shape:
{{
  "coverage_status": "Covered" | "Partially Covered" | "Not Covered" | "SOP Missing",
  "sop_id": <the sop_id number that is most relevant, or null if none are relevant>,
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
        # Deterministic offline fallback. Since matching is no longer
        # category-pre-filtered, this heuristic now has to judge topical
        # relevance itself, not just score overlap between two already-related
        # texts. A generic bag-of-words overlap score is *not* good enough for
        # that on its own -- a long, real SOP shares plenty of generic GMP
        # vocabulary ("procedure", "quality", "documented", "batch"...) with
        # almost any requirement, regardless of actual topic. So: first gate on
        # whether the requirement's own topic words (from its process_area /
        # sop_category, which name the subject matter, not generic GMP filler)
        # appear in the SOP text at all; only compute Covered/Partial/Not
        # Covered once that relevance gate passes.
        best, best_overlap, best_relevant = None, -1.0, False
        for e in sop_excerpts:
            relevant = _topic_relevant(requirement, e["text"])
            overlap = _keyword_overlap(requirement["requirement_text"], e["text"])
            # Prefer any relevant candidate over a non-relevant one, then by overlap.
            if (relevant, overlap) > (best_relevant, best_overlap):
                best, best_overlap, best_relevant = e, overlap, relevant

        if not best_relevant:
            return {
                "coverage_status": "SOP Missing",
                "sop_id": None,
                "cited_text": "",
                "rationale": (
                    f"[Offline heuristic mode - no ANTHROPIC_API_KEY configured] None of the checked SOPs "
                    f"mention terms tied to this requirement's own subject matter ('{requirement.get('process_area','')}' / "
                    f"'{requirement.get('sop_category','')}'), so none look genuinely relevant to this requirement. "
                    "This is a placeholder assessment; connect a live Claude API key for real semantic analysis."
                ),
                "ai_mock": True,
            }
        overlap = best_overlap
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
                f"requirement and '{best['title']}' is {overlap:.0%}. "
                "This is a placeholder assessment; connect a live Claude API key for real semantic analysis."
            ),
            "ai_mock": True,
        }


def _topic_relevant(requirement, sop_text):
    """Relevance gate for the offline heuristic: does the SOP text contain any
    of the distinctive topic words from this requirement's process_area or
    sop_category (the words that actually name its subject matter), beyond
    generic GMP filler that appears in virtually every SOP regardless of
    topic? This is a coarse stand-in for what a live model judges directly
    from context -- it only needs to catch the obvious case (a visual
    inspection SOP has no business being "relevant" to a facility-HVAC
    requirement), not nuance."""
    generic = {
        "the", "and", "of", "to", "for", "a", "in", "or", "with", "is", "are", "be", "that", "shall", "must", "on",
        "procedure", "procedures", "quality", "product", "products", "process", "processes", "system", "systems",
        "documented", "document", "documentation", "personnel", "training", "written", "control", "controls",
        "record", "records", "requirement", "requirements", "including", "appropriate", "ensure", "ensures",
        "area", "areas", "activity", "activities", "manufacturing", "operation", "operations", "compliance",
        "standard", "standards", "applicable", "manufacture", "manufactured", "management", "site", "sites",
    }
    topic_source = f"{requirement.get('process_area','')} {requirement.get('sop_category','')}"
    topic_words = set(w.lower().strip(".,()/&") for w in topic_source.split() if len(w) > 3 and w.lower() not in generic)
    if not topic_words:
        return True  # no distinguishing topic words to gate on -- don't block, let overlap scoring decide
    text_lower = sop_text.lower()
    return any(w in text_lower for w in topic_words)


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


# ---------------------------------------------------------------------------
# Task 4: Discovery -- suggest regulatory topics not in the curated library
# ---------------------------------------------------------------------------
def discover_uncovered_topics(sop_title, sop_category, sop_text, existing_requirement_summaries):
    """
    Reads an SOP's actual content and suggests regulatory topics/expectations it
    would typically need to address that are NOT already tracked in the curated
    requirements library. This is deliberately separate from RTM: these are
    unverified candidates for a human to review, never an official finding --
    a fixed, curated requirement library can never claim to be complete, and an
    LLM inventing "regulatory requirements" with no human review would be a
    worse failure mode (an untraceable, unvetted compliance claim) than a known
    gap in the library.

    existing_requirement_summaries: list of short strings describing what's
    already tracked for this SOP's category, so the model doesn't just
    re-suggest things already in the system.

    returns: {"candidates": [...], "ai_mock": bool, "offline_note": str|None}
    """
    system_prompt = (
        "You are a pharmaceutical GMP compliance analyst. You are given the text of an SOP and a list of "
        "regulatory requirements this platform already tracks for its category. Identify SPECIFIC regulatory "
        "topics or expectations -- from FDA 21 CFR 210/211, EU GMP Annex 1, WHO GMP, ICH guidelines, USP "
        "chapters, PDA technical reports, or PIC/S -- that this SOP's subject matter would typically need to "
        "address, but that are NOT already covered by the tracked requirements listed below. Only suggest "
        "something if you can name a specific standard/chapter/section it comes from -- do not invent vague "
        "'best practices' with no citable source. If genuinely nothing looks missing, return an empty list. "
        "Respond with strict JSON only, no prose outside the JSON."
    )
    existing_block = "\n".join(f"- {s}" for s in existing_requirement_summaries) or "(none tracked yet for this category)"
    user_prompt = f"""SOP: {sop_title} (category: {sop_category})
SOP text (excerpt):
{sop_text[:6000]}

Already-tracked requirements for this SOP's category:
{existing_block}

Respond with JSON exactly in this shape:
{{
  "candidates": [
    {{
      "topic": "<short topic name, e.g. 'Sub-visible particulate testing'>",
      "suggested_source": "<specific standard, e.g. 'USP <788>' -- must be a real, nameable source>",
      "suggested_clause": "<clause/section if you know it, else empty string>",
      "suggested_category": "<a short category label consistent with the SOP's own category style>",
      "rationale": "<1-2 sentences: why this SOP's subject matter would need to address this, and why it looks missing from the tracked list>"
    }}
  ]
}}"""
    try:
        # 1200 tokens was too tight for this response shape -- a handful of
        # candidates, each with topic/source/clause/category/rationale, could
        # exceed it and get cut off mid-JSON (hitting max_tokens truncates the
        # response, not just wraps it, so the JSON is genuinely incomplete).
        # That was surfacing as an unhandled JSONDecodeError in production
        # rather than degrading gracefully. 3000 gives real headroom.
        raw = _call_claude(system_prompt, user_prompt, max_tokens=3000)
        parsed = _extract_json_object(raw)
        candidates = parsed.get("candidates")
        if not isinstance(candidates, list):
            raise AIError("missing_candidates_list_in_response")
        return {"candidates": candidates, "ai_mock": False, "offline_note": None}
    except AIError as e:
        # Unlike RTM coverage, there is no sensible non-AI substitute for
        # open-ended "what regulatory topic haven't we thought to check for" --
        # a keyword-overlap heuristic can rank existing candidates but can't
        # invent new ones. Be explicit about that instead of faking a result.
        return {
            "candidates": [],
            "ai_mock": True,
            "offline_note": (
                f"Discovery requires a live Claude API key ({e}) -- there is no offline heuristic for "
                "open-ended gap discovery, unlike RTM coverage assessment which has a keyword-overlap "
                "fallback. Configure ANTHROPIC_API_KEY to use this feature."
            ),
        }


# ---------------------------------------------------------------------------
# Task 5: General review -- writing quality and completeness, independent of
# any specific regulatory requirement
# ---------------------------------------------------------------------------
def review_sop_quality(sop_title, sop_text):
    """
    Reviews an SOP for issues that aren't tied to any single regulatory
    requirement: grammar, spelling, and sentence-formation problems, plus
    whether the document references or clearly needs supporting forms/
    templates/annexures that aren't present. This is deliberately separate
    from the regulatory RTM assessment -- a sentence can be grammatically
    broken or a referenced form missing regardless of whether the SOP is
    otherwise regulatorily compliant, and conflating the two would bury
    genuine compliance gaps under copy-editing notes (or vice versa).

    returns: {"issues": [...], "ai_mock": bool, "offline_note": str|None}
    Each issue: {issue_type, location, current_text, proposed_correction, why_needed}
    issue_type is one of "Grammar", "Spelling", "Sentence Formation", "Missing Form/Template".
    """
    system_prompt = (
        "You are a pharmaceutical SOP editor doing a writing-quality and completeness review -- NOT a "
        "regulatory compliance review. Read the SOP text and identify concrete issues in two categories only: "
        "(1) grammar, spelling, or sentence-formation problems that make an instruction unclear or incorrect, "
        "and (2) forms, templates, or annexures the SOP's own text references or clearly implies but that "
        "are not present in what you were given (e.g. 'record results on Format XYZ' with no such format "
        "included). Do not comment on regulatory compliance -- that is handled separately. Only flag real, "
        "specific issues with an exact quote from the text; do not invent generic style preferences. If the "
        "SOP is clean, return an empty list. Respond with strict JSON only, no prose outside the JSON."
    )
    user_prompt = f"""SOP: {sop_title}
SOP text (excerpt):
{sop_text[:8000]}

Respond with JSON exactly in this shape:
{{
  "issues": [
    {{
      "issue_type": "Grammar" | "Spelling" | "Sentence Formation" | "Missing Form/Template",
      "location": "<short pointer, e.g. 'Section 4.2, second paragraph'>",
      "current_text": "<short exact quote of the problematic text, or the SOP's reference to the missing form>",
      "proposed_correction": "<the corrected text, or what form/template should be added>",
      "why_needed": "<1 sentence: why this needs fixing>"
    }}
  ]
}}"""
    try:
        raw = _call_claude(system_prompt, user_prompt, max_tokens=3000)
        parsed = _extract_json_object(raw)
        issues = parsed.get("issues")
        if not isinstance(issues, list):
            raise AIError("missing_issues_list_in_response")
        return {"issues": issues, "ai_mock": False, "offline_note": None}
    except AIError as e:
        # Like discovery, there is no sensible offline substitute for judging
        # writing quality or spotting an implied-but-missing form -- a
        # keyword heuristic can't do either reliably. Be explicit rather than
        # faking a result.
        return {
            "issues": [],
            "ai_mock": True,
            "offline_note": (
                f"General writing/completeness review requires a live Claude API key ({e}) -- there is no "
                "offline heuristic for this. Configure ANTHROPIC_API_KEY to use this feature."
            ),
        }
