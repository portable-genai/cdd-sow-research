"""Deterministic fuzzy name matching for sanctions/PEP screening.

A pure, dependency-free matcher: normalise names (case, punctuation, accents, common
org suffixes), score similarity with Jaro-Winkler plus a token-set ratio (handling word
order and partial overlaps), and combine with an optional date-of-birth check. The same
inputs always yield the same score, so a screening alert is reproducible and an auditor
can recompute it. No LLM, no I/O.

This mirrors how OFAC's own SDN Search applies fuzzy logic with an adjustable threshold;
the default here (0.85) is a sensible starting point and is configurable on the service.
"""

from __future__ import annotations

import re
import unicodedata

# Organisation suffixes stripped before comparison (so "Acme Ltd" ~ "Acme Limited").
_ORG_SUFFIXES = {
    "ltd",
    "limited",
    "llc",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "plc",
    "pte",
    "pty",
    "gmbh",
    "ag",
    "sa",
    "nv",
    "bv",
    "srl",
    "spa",
    "holdings",
    "group",
    "trust",
    "foundation",
    "llp",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize(name: str) -> str:
    """Lower-case, strip accents and punctuation, collapse whitespace."""
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return " ".join(_TOKEN_RE.findall(ascii_only.lower()))


def tokens(name: str, *, drop_org_suffixes: bool = True) -> list[str]:
    toks = normalize(name).split()
    if drop_org_suffixes:
        stripped = [t for t in toks if t not in _ORG_SUFFIXES]
        if stripped:
            return stripped
    return toks


def jaro_winkler(a: str, b: str) -> float:
    """Jaro-Winkler similarity in [0, 1] (pure implementation)."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    la, lb = len(a), len(b)
    window = max(la, lb) // 2 - 1
    if window < 0:
        window = 0
    a_match = [False] * la
    b_match = [False] * lb
    matches = 0
    for i in range(la):
        lo = max(0, i - window)
        hi = min(i + window + 1, lb)
        for j in range(lo, hi):
            if not b_match[j] and a[i] == b[j]:
                a_match[i] = b_match[j] = True
                matches += 1
                break
    if matches == 0:
        return 0.0
    # Count transpositions.
    k = 0
    transpositions = 0
    for i in range(la):
        if a_match[i]:
            while not b_match[k]:
                k += 1
            if a[i] != b[k]:
                transpositions += 1
            k += 1
    transpositions //= 2
    m = matches
    jaro = (m / la + m / lb + (m - transpositions) / m) / 3.0
    # Winkler prefix bonus (up to 4 chars).
    prefix = 0
    for i in range(min(4, la, lb)):
        if a[i] == b[i]:
            prefix += 1
        else:
            break
    return jaro + prefix * 0.1 * (1 - jaro)


def _coverage(source: list[str], target: list[str]) -> float:
    """How well ``target`` accounts for every token of ``source`` (mean best match)."""
    return sum(max(jaro_winkler(t, u) for u in target) for t in source) / len(source)


def token_set_ratio(a: str, b: str) -> float:
    """Order-independent token similarity, measured in BOTH directions.

    Scoring only how well the shorter name is covered makes every subset a perfect
    match: with organisation suffixes stripped, "Trust Logistics LLC" reduces to the
    single token "logistics", which is fully covered by "Meridian Logistics Holdings",
    so two unrelated companies scored 1.00. Against a real 20,000-entry watchlist that
    is not a conservative bias, it is noise: it fires a certain-match alert on any name
    sharing one generic industry word, and an alert list nobody trusts is how a genuine
    hit gets waved through.

    So take the weaker of the two directions: a match must account for the query AND for
    the watchlist name. Exact names and short forms still score 1.00, because entries
    carry their short forms as aliases and every alias is scored separately (see
    ``best_name_score`` and ``ScreeningService._match``); what this removes is the claim
    of certainty about a name the overlap only partly explains.
    """
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return min(_coverage(ta, tb), _coverage(tb, ta))


def name_score(a: str, b: str) -> float:
    """Combined name similarity: max of whole-string and token-set Jaro-Winkler."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    return max(jaro_winkler(na, nb), token_set_ratio(a, b))


def _year(dob: str | None) -> str:
    return dob[:4] if dob and len(dob) >= 4 and dob[:4].isdigit() else ""


def dob_agreement(query: str | None, candidate: str | None) -> float | None:
    """+/-/neutral DOB signal: 1.0 exact, 0.5 same year, 0.0 conflict, None unknown."""
    if not query or not candidate:
        return None
    if query == candidate:
        return 1.0
    qy, cy = _year(query), _year(candidate)
    if qy and cy:
        return 0.5 if qy == cy else 0.0
    return None


def best_name_score(query: str, names: list[str]) -> tuple[float, str]:
    """Return the best (score, matched_name) of ``query`` against ``names``."""
    best = (0.0, "")
    for n in names:
        s = name_score(query, n)
        if s > best[0]:
            best = (s, n)
    return best
