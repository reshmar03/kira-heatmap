import base64
import hashlib
import json
import os
import random
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import anthropic
from pydantic import BaseModel, Field


CLAUSES = [
    "assignment",
    "change_of_control",
    "termination",
    "limitation_of_liability",
    "indemnification",
    "confidentiality",
    "governing_law",
    "non_compete",
    "exclusivity",
    "most_favored_nation",
    "audit_rights",
    "insurance",
    "data_protection",
    "intellectual_property",
    "force_majeure",
    "notice",
]

# Bump this whenever the extraction/comparison schema or prompt changes in a
# way that should invalidate previously cached results.
SCHEMA_VERSION = "v3-claude"

CACHE_DIR = Path(os.getenv("KIRA_CACHE_DIR", ".cache"))

# Retry policy for transient Claude errors (5xx/overloaded/rate-limited).
RETRY_BACKOFF_SECONDS = [2, 4, 8, 16]
MAX_RETRY_ATTEMPTS = len(RETRY_BACKOFF_SECONDS)

EXTRACTION_MAX_TOKENS = 4096
COMPARISON_MAX_TOKENS = 8192

_EXTRACTION_TOOL_NAME = "record_contract_analysis"
_COMPARISON_TOOL_NAME = "record_comparison"

OnAttempt = Optional[Callable[[int, int, Exception], None]]


class Clause(BaseModel):
    name: str
    present: bool
    text: Optional[str] = None
    page: Optional[int] = None


class ContractAnalysis(BaseModel):
    contract_name: str
    clauses: List[Clause] = Field(default_factory=list)


class Comparison(BaseModel):
    clause: str
    status: str = Field(
        description="One of: same, modified, major_deviation, missing, added"
    )
    form_text: Optional[str] = None
    form_page: Optional[int] = None
    contract_text: Optional[str] = None
    contract_page: Optional[int] = None
    explanation: str


class ComparisonSet(BaseModel):
    contract_name: str
    comparisons: List[Comparison]


# --------------------------------------------------------------------------
# Demo mode
# --------------------------------------------------------------------------


def demo_mode_enabled() -> bool:
    return os.getenv("DEMO_MODE", "false").strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# Retry handling
# --------------------------------------------------------------------------


def _is_retryable_error(exc: Exception) -> bool:
    """Only retry transient server-side/rate-limit errors.

    Authentication errors, invalid-model errors, and malformed/invalid-request
    errors (4xx other than 429) are never retried.
    """
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500 or exc.status_code == 429
    return False


def _call_with_retry(fn: Callable[[], dict], on_attempt: OnAttempt = None) -> dict:
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - re-raised below when not retryable
            if not _is_retryable_error(exc) or attempt >= MAX_RETRY_ATTEMPTS:
                raise
            wait_seconds = RETRY_BACKOFF_SECONDS[attempt]
            attempt += 1
            if on_attempt:
                on_attempt(attempt, wait_seconds, exc)
            time.sleep(wait_seconds)


# --------------------------------------------------------------------------
# Local cache
# --------------------------------------------------------------------------


def hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_key(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _cache_path(kind: str, key: str) -> Path:
    return CACHE_DIR / kind / f"{key}.json"


def _cache_load(kind: str, key: str) -> Optional[dict]:
    path = _cache_path(kind, key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _cache_save(kind: str, key: str, data: dict) -> None:
    path = _cache_path(kind, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def clear_cache() -> None:
    if CACHE_DIR.exists():
        shutil.rmtree(CACHE_DIR)


# --------------------------------------------------------------------------
# Numeric deviation detection
# --------------------------------------------------------------------------

_NUMERIC_LABELS = {
    "limitation_of_liability": "Liability cap",
    "insurance": "Insurance limit",
    "notice": "Notice period",
}

_DOLLAR_RE = re.compile(r"\$\s?[\d,]+(?:\.\d+)?")
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?\s?%")
_DURATION_RE = re.compile(r"\b(\d+)\s*(day|days|month|months|year|years)\b", re.IGNORECASE)


def _parse_dollar(raw: str) -> float:
    return float(raw.replace("$", "").replace(",", "").strip())


def _parse_percent(raw: str) -> float:
    return float(raw.replace("%", "").strip())


def _find_numeric_token(text: Optional[str]):
    if not text:
        return None
    match = _DOLLAR_RE.search(text)
    if match:
        return ("dollar", _parse_dollar(match.group()), match.group().strip())
    match = _PERCENT_RE.search(text)
    if match:
        return ("percent", _parse_percent(match.group()), match.group().strip())
    match = _DURATION_RE.search(text)
    if match:
        unit = match.group(2).lower().rstrip("s")
        return (f"duration:{unit}", int(match.group(1)), match.group().strip())
    return None


def describe_numeric_deviation(
    clause: str, form_text: Optional[str], contract_text: Optional[str]
) -> Optional[str]:
    """Detect a comparable numeric value (dollar amount, percentage, or
    day/month/year duration) in both texts and describe the change in
    deterministic, human-readable terms."""
    form_token = _find_numeric_token(form_text)
    contract_token = _find_numeric_token(contract_text)
    if not form_token or not contract_token:
        return None

    form_kind, form_value, form_raw = form_token
    contract_kind, contract_value, contract_raw = contract_token
    if form_kind != contract_kind or form_value == contract_value:
        return None

    label = _NUMERIC_LABELS.get(clause, clause.replace("_", " ").capitalize())
    direction = "decreased" if contract_value < form_value else "increased"

    if form_kind in ("dollar", "percent"):
        return (
            f"{label} {direction} from {form_raw} in the form to {contract_raw} "
            "in the contract."
        )

    unit = form_kind.split(":", 1)[1]
    form_unit = unit if form_value == 1 else f"{unit}s"
    contract_unit = unit if contract_value == 1 else f"{unit}s"
    return (
        f"{label} {direction} from {form_value} {form_unit} in the form to "
        f"{contract_value} {contract_unit} in the contract."
    )


def _apply_numeric_deviation(item: dict) -> dict:
    explanation = describe_numeric_deviation(
        item.get("clause", ""), item.get("form_text"), item.get("contract_text")
    )
    if explanation:
        item["explanation"] = explanation
        if item.get("status") == "same":
            item["status"] = "modified"
    return item


# --------------------------------------------------------------------------
# Demo-mode mock generation
# --------------------------------------------------------------------------

_DEMO_CLAUSE_TEXT = {
    "limitation_of_liability": lambda rng: (
        f"Total liability shall not exceed "
        f"${rng.choice([250000, 500000, 750000, 1000000, 1500000]):,}."
    ),
    "insurance": lambda rng: (
        f"Vendor shall maintain commercial general liability insurance of at "
        f"least ${rng.choice([1000000, 2000000, 5000000]):,}."
    ),
    "notice": lambda rng: (
        f"Either party may terminate upon {rng.choice([30, 45, 60, 90])} days "
        "prior written notice."
    ),
    "non_compete": lambda rng: (
        f"Restrictions apply for {rng.choice([1, 2, 3])} years following "
        "termination."
    ),
    "termination": lambda rng: (
        f"This agreement may be terminated with {rng.choice([30, 60, 90])} "
        "days notice for convenience."
    ),
    "most_favored_nation": lambda rng: (
        f"Pricing shall be no less favorable, with adjustment thresholds of "
        f"{rng.choice([5, 10, 15])}%."
    ),
}


def _demo_clause_text(clause: str, rng: random.Random) -> str:
    generator = _DEMO_CLAUSE_TEXT.get(clause)
    if generator:
        return generator(rng)
    return f"Standard {clause.replace('_', ' ')} provision text (demo)."


def _demo_contract_analysis(path: str, pdf_hash: str) -> dict:
    rng = random.Random(pdf_hash)
    clauses = []
    for clause in CLAUSES:
        present = rng.random() > 0.12
        text = _demo_clause_text(clause, rng) if present else None
        page = rng.randint(1, 24) if present else None
        clauses.append({"name": clause, "present": present, "text": text, "page": page})
    return {
        "contract_name": Path(path).name,
        "clauses": clauses,
        "pdf_hash": pdf_hash,
    }


_DEMO_EXPLANATIONS = {
    "same": "Demo mode: clause text is substantively equivalent.",
    "modified": "Demo mode: clause text differs but is not a major departure.",
    "major_deviation": "Demo mode: clause represents a material departure from the form.",
    "missing": "Demo mode: form includes this clause; contract does not.",
    "added": "Demo mode: contract includes this clause; form does not.",
}


def _demo_comparison(form: dict, contract: dict) -> dict:
    seed = f"{form.get('pdf_hash')}:{contract.get('pdf_hash')}"
    rng = random.Random(seed)
    form_clauses = {c["name"]: c for c in form.get("clauses", [])}
    contract_clauses = {c["name"]: c for c in contract.get("clauses", [])}

    statuses = ["same", "modified", "major_deviation"]
    weights = [0.5, 0.3, 0.2]

    comparisons = {}
    for clause in CLAUSES:
        f = form_clauses.get(clause, {})
        c = contract_clauses.get(clause, {})
        f_present = f.get("present", False)
        c_present = c.get("present", False)

        if f_present and not c_present:
            status = "missing"
        elif c_present and not f_present:
            status = "added"
        elif not f_present and not c_present:
            status = "same"
        else:
            status = rng.choices(statuses, weights=weights)[0]

        comparisons[clause] = {
            "clause": clause,
            "status": status,
            "form_text": f.get("text"),
            "form_page": f.get("page"),
            "contract_text": c.get("text"),
            "contract_page": c.get("page"),
            "explanation": _DEMO_EXPLANATIONS[status],
        }

    for item in comparisons.values():
        _apply_numeric_deviation(item)

    return {
        "contract_name": contract.get("contract_name", "Contract"),
        "comparisons": comparisons,
    }


# --------------------------------------------------------------------------
# Claude client
# --------------------------------------------------------------------------


def _client() -> anthropic.Anthropic:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    # max_retries=0: our own _call_with_retry owns the retry/backoff policy.
    return anthropic.Anthropic(api_key=key, max_retries=0)


def _default_model() -> str:
    return os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")


def _extraction_tool() -> dict:
    return {
        "name": _EXTRACTION_TOOL_NAME,
        "description": "Record the extracted contract-review provisions.",
        "input_schema": ContractAnalysis.model_json_schema(),
    }


def _comparison_tool() -> dict:
    return {
        "name": _COMPARISON_TOOL_NAME,
        "description": "Record the clause-by-clause comparison between the form and the contract.",
        "input_schema": ComparisonSet.model_json_schema(),
    }


def _extract_tool_input(response, tool_name: str) -> dict:
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input
    raise RuntimeError("Claude did not return the expected structured tool call.")


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def analyze_contract(
    path: str,
    model: Optional[str] = None,
    on_retry: OnAttempt = None,
) -> dict:
    model = model or _default_model()
    pdf_hash = hash_file(path)
    cache_key = _cache_key("extract", pdf_hash, model, SCHEMA_VERSION)

    cached = _cache_load("extract", cache_key)
    if cached is not None:
        return cached

    if demo_mode_enabled():
        result = _demo_contract_analysis(path, pdf_hash)
        _cache_save("extract", cache_key, result)
        return result

    def _do() -> dict:
        client = _client()
        with open(path, "rb") as f:
            pdf_b64 = base64.standard_b64encode(f.read()).decode("ascii")

        prompt = f"""
Analyze this legal agreement as a contract-review extraction task.

Extract these provisions:
{", ".join(CLAUSES)}

For every provision:
- determine whether it is present
- quote the most relevant provision text, keeping it reasonably concise
- when the provision contains a dollar amount, percentage, or a duration
  (days/months/years), quote that value exactly as written, including the
  currency symbol, percent sign, or unit
- give the page number where it appears
- if it is absent, set present=false and text/page to null
- do not invent provisions
- distinguish a true clause from a mere reference to another section

Call the record_contract_analysis tool with the extracted data.
"""

        response = client.messages.create(
            model=model,
            max_tokens=EXTRACTION_MAX_TOKENS,
            tools=[_extraction_tool()],
            tool_choice={"type": "auto"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )

        tool_input = _extract_tool_input(response, _EXTRACTION_TOOL_NAME)
        parsed = ContractAnalysis.model_validate(tool_input)
        result = parsed.model_dump()
        result["contract_name"] = Path(path).name
        result["pdf_hash"] = pdf_hash
        return result

    result = _call_with_retry(_do, on_attempt=on_retry)
    _cache_save("extract", cache_key, result)
    return result


def compare_to_form(
    form: dict,
    contract: dict,
    model: Optional[str] = None,
    on_retry: OnAttempt = None,
) -> dict:
    model = model or _default_model()
    form_hash = form.get("pdf_hash", "unknown-form")
    contract_hash = contract.get("pdf_hash", "unknown-contract")
    cache_key = _cache_key("compare", form_hash, contract_hash, model, SCHEMA_VERSION)

    cached = _cache_load("compare", cache_key)
    if cached is not None:
        return cached

    if demo_mode_enabled():
        result = _demo_comparison(form, contract)
        _cache_save("compare", cache_key, result)
        return result

    def _do() -> dict:
        client = _client()

        prompt = f"""
Compare the following extracted form-agreement provisions with the extracted
provisions from a contract.

FORM:
{json.dumps(form, ensure_ascii=False)}

CONTRACT:
{json.dumps(contract, ensure_ascii=False)}

For every clause in this list:
{json.dumps(CLAUSES)}

Use exactly one status:
- same: substantively equivalent
- modified: changed, but not clearly a major commercial/legal departure
- major_deviation: material departure from the form
- missing: form has it but contract does not
- added: contract has it but form does not

Base the result on the supplied text. Do not invent language.

Pay particular attention to numeric values (dollar amounts, percentages,
liability caps, insurance limits, notice periods, and durations in days,
months, or years). If a numeric value changed, the explanation must state
the specific old and new values.

For each clause provide a short explanation identifying the actual difference.

Call the record_comparison tool with the result.
"""

        response = client.messages.create(
            model=model,
            max_tokens=COMPARISON_MAX_TOKENS,
            tools=[_comparison_tool()],
            tool_choice={"type": "auto"},
            messages=[{"role": "user", "content": prompt}],
        )

        tool_input = _extract_tool_input(response, _COMPARISON_TOOL_NAME)
        parsed = ComparisonSet.model_validate(tool_input)
        comparisons = {item.clause: item.model_dump() for item in parsed.comparisons}

        return {
            "contract_name": contract.get("contract_name", "Contract"),
            "comparisons": comparisons,
        }

    result = _call_with_retry(_do, on_attempt=on_retry)

    for item in result["comparisons"].values():
        _apply_numeric_deviation(item)

    _cache_save("compare", cache_key, result)
    return result
