"""Was a defect fixed before or after release? The origin of a bug-fix PR, with a confidence and the evidence behind it.

Change failure rate (DORA) counts changes that reached production and needed remediation (hotfix, rollback, fix-forward). A
bug-fix PR does not say which: it can fix a defect that escaped, or one found in review, QA or CI before anything shipped. Tracker
data rarely records this, so the classification is an evidence-weighted estimate, and it is conservative the way the metric is:

* A defect PR is **assumed post-release (escaped)** unless the evidence says otherwise, with ``low`` confidence ("assumed").
* Evidence weighs on one side or the other: 3 for a strong signal (a hotfix label or branch, "found in QA", a fix aimed at an
  unreleased feature branch, a revert), 2 for a medium one ("regression", a CI fix), 1 for a weak one (the target is main or a
  release branch). The side with more weight wins; no evidence or a tie goes to escaped.
* Confidence is the winner's weight and its margin over the other side: ``high`` (3+ and a margin of 3+), ``medium`` (2+ and a
  margin of 2+), otherwise ``low``. Reports show the counts per confidence, so a rate built on assumptions says so.

Revert PRs are defects too (a rollback is the textbook change failure). Only PRs of type ``bug`` / ``security`` (or reverts) are
classified; everything else has no origin. Improve the estimate by labelling hotfixes (``hotfix``, ``incident``, ``regression``) and
by writing "found in QA / review" in the description of pre-release fixes: the classifier reads both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ESCAPED, PRE_RELEASE = "escaped", "pre_release"
DEFECT_TYPES = ("bug", "security")

# (weight, pattern) per side, matched against the title + linked-issue summary ("headline") and the description ("body"). Labels are
# matched as whole normalized names, branches by prefix: a substring is not evidence ("unsupported-platform" is not a support case).
_ESCAPED_TEXT = [
    (3, re.compile(r"\bhot-?fix\b|\bin prod(uction)?\b|\bprod(uction)? (bug|issue|incident|outage|failure|defect)\b|\bcustomer[- ](reported|facing|impact)|"
                   r"\breported by (a |the )?customers?\b|\bincident\b|\boutage\b|\brolled back\b|\broll(ing)? back\b|\bpost-?release\b|"
                   r"\bafter (the )?release\b|\bsev ?[0-2]\b", re.I)),
    (2, re.compile(r"\bregression\b|\bbroke\b|\bbroken (in|by|since)\b|\bsince (v?\d|the release)|\bsupport (ticket|case)\b|\bescalated\b|\bsmoke test failure\b", re.I)),
]
_ESCAPED_LABELS = {
    3: {"hotfix", "incident", "outage", "rollback", "escaped", "production", "prod", "customer", "customer-reported", "customer-impact", "sev0", "sev1", "sev2", "sev-0", "sev-1", "sev-2"},
    2: {"regression", "p0", "p1", "support", "support-escalation"},
}
_ESCAPED_BRANCH = [(3, re.compile(r"^(hot-?fix|emergency)[/_-]", re.I))]
_ESCAPED_TARGET = [(1, re.compile(r"^(main|master|prod(uction)?|release([/_-].*)?|stable|hot-?fix[/_-].*)$", re.I))]

_PRE_TEXT = [
    (3, re.compile(r"\bpre-?release\b|\bbefore (the )?release\b|\bnot yet released\b|\bunreleased (feature|code|version|build|change)s?\b|"
                   r"\bfound (in|during|by) (qa|testing|review|code review|staging|dev|ci)\b|\bqa[- ]found\b|\bcaught (in|by|during) (ci|review|qa|testing)\b|"
                   r"\baddress(ed|ing)? (the )?(pr |code )?review (feedback|comments?)\b|\breview feedback\b|\bpr feedback\b|\bin the same (pr|release|sprint)\b", re.I)),
    (2, re.compile(r"\bfix(es|ing)? (the )?(broken |failing )?(ci|lint|linting|formatting)\b|\bfix(es|ing)? (the )?(broken|failing) (build|pipeline)\b|"
                   r"\bfailing (build|pipeline)\b|\bci failure\b|\bfollow-?up (to|of|for) #?\d+", re.I)),
]
_PRE_LABELS = {3: {"pre-release", "prerelease", "found-in-qa", "found-in-testing", "found-in-review", "found-in-staging", "qa-found", "dev-only"}}
_PRE_BRANCH = [(2, re.compile(r"(review|feedback|comments?)[-_/]?(fix|fixes|address)|address[-_/](review|comments?|feedback)", re.I))]
# a fix aimed at an integration / feature branch fixes work that has not shipped yet
_PRE_TARGET = [(3, re.compile(r"^(feature|feat|epic|topic|wip|spike|integration)[/_-]", re.I))]

# A git revert ("Revert "add x"", "Revert #12", "Revert 1a2b3c4") or an explicit rollback of a change; not "Rollback feature flag docs".
_REVERT = re.compile(r"""^\s*(revert|rollback)(\s+["'“‘]|\s+(of\s+)?(#\d+|pr\s*#?\d+|pull request|commit|merge)\b|\s+[0-9a-f]{7,40}\b)""", re.I)
_UNCHECKED_BOX = re.compile(r"(?m)^\s*[-*+]\s*\[ \].*$")   # a PR template's unticked option is not evidence


@dataclass(frozen=True)
class Origin:
    kind: str                       # ESCAPED | PRE_RELEASE
    confidence: str                 # high | medium | low
    assumed: bool                   # no evidence for either side: the default applies
    signals: tuple[str, ...] = field(default_factory=tuple)


def _labels(pr: dict) -> set[str]:
    """Label names normalized for exact matching: lower case, spaces/underscores as '-', and the last segment of 'scope/name' or 'scope: name'."""
    raw = pr.get("labels") or pr.get("pr_labels") or []
    names: set[str] = set()
    for x in raw:
        name = x.get("name") if isinstance(x, dict) else x
        if not name:
            continue
        flat = re.sub(r"[\s_]+", "-", str(name).strip().lower())
        names.add(flat)
        names.add(re.split(r"[/:]", flat)[-1].strip("-"))
    return names


def _label_weight(labels: set[str], table: dict[int, set[str]], found: list[str]) -> int:
    for weight in sorted(table, reverse=True):
        hit = sorted(labels & table[weight])
        if hit:
            found.append(f"label: {hit[0]}")
            return weight
    return 0


def is_defect(pr: dict) -> bool:
    """A bug / security fix, or a revert."""
    return pr.get("pr_type") in DEFECT_TYPES or bool(_REVERT.match(pr.get("title") or ""))


def _weigh(rules, text: str, why: str, found: list[str]) -> int:
    best = 0
    for weight, pattern in rules:
        m = pattern.search(text)
        if m and weight > best:
            best = weight
            hit = f"{why}: {m.group(0).strip()[:30]}"
            found[:] = [s for s in found if not s.startswith(f"{why}:")] + [hit]
    return best


def classify(pr: dict) -> Origin | None:
    """The origin of a defect PR, or None when the PR is not a defect fix."""
    if not is_defect(pr):
        return None
    body = _UNCHECKED_BOX.sub("", str(pr.get("description") or pr.get("body") or ""))
    text = " ".join([str(pr.get("title") or ""), str(pr.get("jira_summary") or ""), body])
    labels = _labels(pr)
    branch = pr.get("branch") or pr.get("source_branch") or pr.get("headRefName") or ""
    target = pr.get("target_branch") or pr.get("baseRefName") or ""

    esc_sig: list[str] = []
    pre_sig: list[str] = []
    escaped = _weigh(_ESCAPED_TEXT, text, "text", esc_sig) + _label_weight(labels, _ESCAPED_LABELS, esc_sig)
    escaped += _weigh(_ESCAPED_BRANCH, branch, "branch", esc_sig) + _weigh(_ESCAPED_TARGET, target, "target", esc_sig)
    pre = _weigh(_PRE_TEXT, text, "text", pre_sig) + _label_weight(labels, _PRE_LABELS, pre_sig)
    pre += _weigh(_PRE_BRANCH, branch, "branch", pre_sig) + _weigh(_PRE_TARGET, target, "target", pre_sig)
    if _REVERT.match(pr.get("title") or ""):   # a revert is one piece of evidence, not an addition to the wording's
        escaped = max(escaped, 3)
        esc_sig.append("revert")

    kind = PRE_RELEASE if pre > escaped else ESCAPED   # no evidence, or a tie: the default (escaped) applies
    win, lose = (pre, escaped) if kind == PRE_RELEASE else (escaped, pre)
    margin = win - lose
    confidence = "high" if win >= 3 and margin >= 3 else ("medium" if win >= 2 and margin >= 2 else "low")
    assumed = kind == ESCAPED and escaped < 2   # no evidence, or only the weak "target is main" hint
    return Origin(kind, confidence, assumed, tuple(pre_sig if kind == PRE_RELEASE else esc_sig))


def summarize(prs: list[dict]) -> dict:
    """Counts over ``prs``: defects, escaped / pre-release, and the escaped ones by confidence (``assumed`` = no evidence)."""
    out = {"defects": 0, "escaped": 0, "pre_release": 0, "high": 0, "medium": 0, "low": 0, "assumed": 0}
    for pr in prs:
        origin = classify(pr)
        if origin is None:
            continue
        out["defects"] += 1
        out[origin.kind] += 1
        if origin.kind == ESCAPED:
            out[origin.confidence] += 1
            out["assumed"] += origin.assumed
    return out


def cell(pr: dict) -> str:
    """Short table cell: ``prod·H``, ``prod·L (assumed)``, ``pre·M`` or ``—`` for a PR that is not a defect fix."""
    origin = classify(pr)
    if origin is None:
        return "—"
    letter = origin.confidence[0].upper()
    if origin.kind == PRE_RELEASE:
        return f"🧪 pre-release·{letter}"
    return f"🏭 post-release·{letter}" + (" (assumed)" if origin.assumed else "")


def explain(pr: dict) -> str:
    """The evidence in words, for a tooltip or a single-PR row ('' for a non-defect)."""
    origin = classify(pr)
    if origin is None:
        return ""
    why = ", ".join(origin.signals) if origin.signals else "no evidence either way: a defect fix is assumed to fix released code"
    return f"{'Fixed before release' if origin.kind == PRE_RELEASE else 'Fixes released code'} ({origin.confidence} confidence): {why}"
