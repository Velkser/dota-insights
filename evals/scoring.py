"""Grade one transcript against its case. Pure functions, no network.

A case passes when all four dimensions pass:

- **tool**: the model called at least one of ``expected_tools`` and none of ``forbidden_tools``.
- **args**: at least one call to an expected tool is valid against the tool's input schema and
  satisfies every argument matcher whose top-level parameter that tool has.
- **envelope**: at least one call to an expected tool returned a status in ``allowed_status``.
- **answer**: the final answer is non-empty and passes every check in ``checks``, plus an
  automatic check that a non-ok result is acknowledged rather than presented as fact.

Argument matchers (``args`` values), all case-insensitive for text:

- ``hero`` / ``item``: name or list of names; the argument (string or list) must resolve to all
  of them with the server's own resolver, so "PA", "Фантомка" and "Phantom Assassin" all match.
- ``hero_any``: at least one of the listed heroes.
- ``equals``, ``any_of``: exact value or one of several.
- ``contains``, ``contains_any``: substring of the text argument.
- ``includes``: a list argument contains this value.
- ``range``: ``[low, high]`` inclusive for a number.
- ``present``: ``true`` the argument is set, ``false`` it is absent or null.
- ``optional``: ``true`` lets the argument be omitted (the tool default is acceptable); when it
  is set, the other matchers still apply.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from dota_insights.reference import resolve_hero, resolve_item
from evals.dataset import Case
from evals.transcript import ToolCall, Transcript

FLAGS = re.IGNORECASE | re.UNICODE

SAMPLE_SIZE = re.compile(
    r"\d[\d\s,.]*\s*(?:k|тыс\.?)?\s+(?:[\w-]+\s+){0,2}?"
    r"(?:matches|games|match-ups|samples|матч\w*|игр\w*|парти\w*)"
    r"|sample size|sample of|\bn\s*=\s*\d|выборк\w*",
    FLAGS,
)
PATCH = re.compile(r"\b7\.\d{2}[a-z]?\b|\bpatch\b|патч", FLAGS)
CORRELATION_HEDGE = re.compile(
    r"correlat|associat|caus|selection bias|survivo|confound|already (?:ahead|winning)"
    r"|корреляц|связан|причин|смещени|не означает|не обязательно|уже (?:впереди|выигрыва)",
    FLAGS,
)
CAUSAL_CLAIM = re.compile(
    r"\b(?:causes|caused by|guarantees?|makes you win|is the reason (?:you|teams?) win"
    r"|directly increases)\b"
    r"|гарантирует|приводит к победе|является причиной|напрямую повышает",
    FLAGS,
)
# A causal word inside a negation or hedge ("does not cause", "not necessarily causal").
NEGATED_CAUSAL = re.compile(
    r"(?:not|n't|no evidence|cannot|can't|doesn't)\s+(?:\w+\s+){0,3}?(?:caus|guarantee)"
    r"|не\s+(?:\w+\s+){0,2}?(?:гарантир|причин|приводит)",
    FLAGS,
)
LIMITATION = re.compile(
    r"insufficient|not enough|too few|no data|couldn't|could not|unable|unavailable|failed"
    r"|error|limited data|small sample|недостаточно|мало (?:данных|матчей|игр)|нет данных"
    r"|не удалось|ошибк|недоступ",
    FLAGS,
)
NUMBER = re.compile(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(%)?")


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class Dimension:
    passed: bool
    detail: str = ""


@dataclass
class CaseScore:
    case_id: str
    category: str
    lang: str
    tool: Dimension
    args: Dimension
    envelope: Dimension
    answer: Dimension
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(d.passed for d in (self.tool, self.args, self.envelope, self.answer))

    @property
    def score(self) -> float:
        dims = (self.tool, self.args, self.envelope, self.answer)
        return sum(d.passed for d in dims) / len(dims)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "passed": self.passed, "score": self.score}


# --- argument matchers -------------------------------------------------------------------


MISSING = object()


def get_path(arguments: dict[str, Any], path: str) -> Any:
    """Value at a dotted path, or ``MISSING``."""
    value: Any = arguments
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def _norm(value: Any) -> str:
    return str(value).strip().casefold()


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else [value]


def _resolved_ids(values: Iterable[Any], resolve: Callable[[str], Any]) -> set[int]:
    ids = set()
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool):
            ids.add(value)
        elif isinstance(value, str):
            resolution = resolve(value)
            record = resolution.best or (
                resolution.candidates[0].record if resolution.candidates else None
            )
            if record is not None:
                ids.add(record.id)
    return ids


def _names_match(
    value: Any, expected: Any, resolve: Callable[[str], Any], *, need_all: bool
) -> tuple[bool, str]:
    got = _resolved_ids(_as_list(value), resolve)
    want = _resolved_ids(_as_list(expected), resolve)
    ok = want <= got if need_all else bool(want & got)
    return ok, "" if ok else f"{value!r} does not resolve to {expected!r}"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def match_arg(spec: dict[str, Any], value: Any) -> tuple[bool, str]:
    """Check one argument value against a matcher spec. Returns (passed, reason if not)."""
    if "present" in spec:
        is_set = value is not MISSING and value is not None
        if is_set != spec["present"]:
            return False, "should be absent" if is_set else "missing"
        if not is_set:
            return True, ""
    if value is MISSING or value is None:
        return (True, "") if spec.get("optional") else (False, "missing")
    for kind, expected in spec.items():
        if kind in ("present", "optional"):
            continue
        if kind == "hero":
            ok, why = _names_match(value, expected, resolve_hero, need_all=True)
        elif kind == "hero_any":
            ok, why = _names_match(value, expected, resolve_hero, need_all=False)
        elif kind == "item":
            ok, why = _names_match(value, expected, resolve_item, need_all=True)
        elif kind == "equals":
            ok = _norm(value) == _norm(expected)
            why = f"{value!r} != {expected!r}"
        elif kind == "any_of":
            ok = _norm(value) in {_norm(e) for e in expected}
            why = f"{value!r} not in {expected!r}"
        elif kind == "contains":
            ok = _norm(expected) in _norm(value)
            why = f"{value!r} does not contain {expected!r}"
        elif kind == "contains_any":
            ok = any(_norm(e) in _norm(value) for e in expected)
            why = f"{value!r} contains none of {expected!r}"
        elif kind == "includes":
            ok = _norm(expected) in {_norm(v) for v in _as_list(value)}
            why = f"{value!r} does not include {expected!r}"
        elif kind == "range":
            number = _number(value)
            low, high = expected
            ok = number is not None and low <= number <= high
            why = f"{value!r} outside [{low}, {high}]"
        else:
            raise ValueError(f"unknown argument matcher {kind!r}")
        if not ok:
            return False, why
    return True, ""


def schema_errors(schema: dict[str, Any] | None, arguments: dict[str, Any]) -> list[str]:
    if not schema:
        return []
    validator = Draft202012Validator(schema)
    return [
        f"{'.'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in validator.iter_errors(arguments)
    ]


def check_call_args(
    case: Case, call: ToolCall, schema: dict[str, Any] | None
) -> tuple[bool, list[str]]:
    """Schema validity plus the case's matchers that apply to this tool."""
    problems = schema_errors(schema, call.arguments)
    params = set((schema or {}).get("properties", {})) if schema else None
    for path, spec in case.args.items():
        top = path.split(".", 1)[0]
        if params is not None and top not in params:
            continue
        ok, why = match_arg(spec, get_path(call.arguments, path))
        if not ok:
            problems.append(f"{path}: {why}")
    if call.is_error and call.error and "validation" in call.error.casefold():
        problems.append(f"server rejected arguments: {call.error}")
    return not problems, problems


# --- answer checks -----------------------------------------------------------------------


def _find_numbers(data: Any, key: str) -> list[float]:
    found: list[float] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k == key and (n := _number(v)) is not None and not isinstance(v, str):
                found.append(n)
            found += _find_numbers(v, key)
    elif isinstance(data, list):
        for v in data:
            found += _find_numbers(v, key)
    return found


def _ok_envelopes(case: Case, transcript: Transcript, tool: str | None) -> list[dict]:
    names = {tool} if tool else set(case.expected_tools)
    return [
        c.envelope
        for c in transcript.calls
        if c.name in names and c.status == "ok" and c.envelope is not None
    ]


def _regex_check(pattern: re.Pattern[str], label: str) -> Callable[..., Check]:
    def check(spec: dict, case: Case, transcript: Transcript) -> Check:
        found = pattern.search(transcript.answer)
        return Check(label, bool(found), found.group(0) if found else f"no {label} mentioned")

    return check


def _mentions(spec: dict, case: Case, transcript: Transcript) -> Check:
    patterns = spec["patterns"]
    hit = next((p for p in patterns if re.search(p, transcript.answer, FLAGS)), None)
    name = spec.get("label", "mentions_any")
    return Check(name, hit is not None, hit or f"none of {patterns}")


def _not_mentions(spec: dict, case: Case, transcript: Transcript) -> Check:
    hits = [p for p in spec["patterns"] if re.search(p, transcript.answer, FLAGS)]
    return Check(spec.get("label", "not_mentions"), not hits, f"found {hits}" if hits else "")


def _no_causal_claim(spec: dict, case: Case, transcript: Transcript) -> Check:
    text = NEGATED_CAUSAL.sub(" ", transcript.answer)
    claim = CAUSAL_CLAIM.search(text)
    if claim and not CORRELATION_HEDGE.search(transcript.answer):
        return Check("no_causal_claim", False, f"unhedged causal claim: {claim.group(0)!r}")
    return Check("no_causal_claim", True, claim.group(0) + " (hedged)" if claim else "")


def _language(spec: dict, case: Case, transcript: Transcript) -> Check:
    lang = spec.get("lang", case.lang)
    letters = [ch for ch in transcript.answer if ch.isalpha()]
    cyrillic = sum("Ѐ" <= ch <= "ӿ" for ch in letters)
    share = cyrillic / len(letters) if letters else 0.0
    # Hero, item and team names stay in Latin script in Russian answers, so the bar is low.
    ok = share >= 0.3 if lang == "ru" else share <= 0.1
    return Check("language", ok, f"{share:.0%} Cyrillic letters, expected {lang}")


def _field(spec: dict, case: Case, transcript: Transcript) -> Check:
    key = spec["key"]
    envelopes = _ok_envelopes(case, transcript, spec.get("tool"))
    values = [v for env in envelopes for v in _find_numbers(env, key)]
    name = f"field:{key}"
    if len(values) < spec.get("min_count", 1):
        return Check(name, False, f"no numeric {key!r} in an ok result")
    low, high = spec.get("range", (float("-inf"), float("inf")))
    bad = [v for v in values if not low <= v <= high]
    if bad:
        return Check(name, False, f"{len(bad)} of {len(values)} outside [{low}, {high}]: {bad[:3]}")
    return Check(name, True, f"{len(values)} value(s) in range")


def _answer_number(spec: dict, case: Case, transcript: Transcript) -> Check:
    low, high = spec["range"]
    percent = spec.get("percent", False)
    numbers = [
        float(m.group(1).replace(",", "."))
        for m in NUMBER.finditer(transcript.answer)
        if m.group(2) or not percent
    ]
    hit = next((n for n in numbers if low <= n <= high), None)
    unit = "%" if percent else ""
    return Check(
        "answer_number",
        hit is not None,
        f"{hit:g}{unit}" if hit is not None else f"no number{unit} in [{low}, {high}]",
    )


CHECKS: dict[str, Callable[[dict, Case, Transcript], Check]] = {
    "sample_size": _regex_check(SAMPLE_SIZE, "sample_size"),
    "patch": _regex_check(PATCH, "patch"),
    "correlation_caveat": _regex_check(CORRELATION_HEDGE, "correlation_caveat"),
    "no_causal_claim": _no_causal_claim,
    "language": _language,
    "mentions_any": _mentions,
    "not_mentions": _not_mentions,
    "field": _field,
    "answer_number": _answer_number,
}


def _acknowledges_non_ok(case: Case, transcript: Transcript) -> Check | None:
    """When no expected tool returned ok, the answer must say the data fell short."""
    expected = [c for c in transcript.calls if c.name in case.expected_tools]
    if not expected or any(c.status == "ok" for c in expected):
        return None
    found = LIMITATION.search(transcript.answer)
    return Check(
        "acknowledges_non_ok",
        bool(found),
        found.group(0) if found else "non-ok result presented without saying so",
    )


# --- scoring -----------------------------------------------------------------------------


def score_case(case: Case, transcript: Transcript, schemas: dict[str, dict[str, Any]]) -> CaseScore:
    """Grade a transcript. ``schemas`` maps tool name -> input JSON schema."""
    called = [c.name for c in transcript.calls]
    expected_calls = [c for c in transcript.calls if c.name in case.expected_tools]
    run_error = f"run failed: {transcript.error}" if transcript.error else ""

    forbidden = sorted(set(called) & set(case.forbidden_tools))
    tool_ok = bool(expected_calls) and not forbidden and not transcript.error
    tool = Dimension(
        tool_ok,
        run_error
        or (f"forbidden tools called: {forbidden}; " if forbidden else "")
        + f"called {called or 'no tools'}, expected one of {list(case.expected_tools)}",
    )

    arg_problems: list[str] = []
    args_ok = False
    for call in expected_calls:
        ok, problems = check_call_args(case, call, schemas.get(call.name))
        if ok:
            args_ok = True
            break
        arg_problems += [f"{call.name}: {p}" for p in problems]
    args = Dimension(
        args_ok and not transcript.error,
        run_error or ("" if args_ok else "; ".join(arg_problems) or "no call to an expected tool"),
    )

    statuses = [
        c.status or ("call_error" if c.is_error else "not_an_envelope") for c in expected_calls
    ]
    envelope_ok = any(c.status in case.allowed_status for c in expected_calls)
    envelope = Dimension(
        envelope_ok and not transcript.error,
        run_error or f"statuses {statuses or 'none'}, allowed {list(case.allowed_status)}",
    )

    checks = [CHECKS[spec["type"]](spec, case, transcript) for spec in case.checks]
    if (auto := _acknowledges_non_ok(case, transcript)) is not None:
        checks.append(auto)
    if not transcript.answer.strip():
        checks.append(Check("non_empty_answer", False, "no final answer"))
    failed = [c for c in checks if not c.passed]
    answer = Dimension(
        not failed and not transcript.error,
        run_error or "; ".join(f"{c.name}: {c.detail}" for c in failed),
    )
    return CaseScore(case.id, case.category, case.lang, tool, args, envelope, answer, checks)
