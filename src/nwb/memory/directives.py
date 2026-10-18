"""USER.md directives (OpenClaw): ``Always`` / ``Never`` / ``Prefer`` with an observed date and a status.

A changed preference marks the old line ``superseded`` in place and writes the new active line under it.
Which verbs and status words are legal comes from config. Conflict is the scope (action kind + field),
computed from the edit diff, so two directives about different fields never overwrite each other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from nwb.conf.models import EditLearningConfig


@dataclass(frozen=True, slots=True)
class Directive:
    verb: str
    text: str
    observed: str  # ISO date, the ask date the edit was approved on
    status: str
    scope: str


def _block_re(cfg: EditLearningConfig) -> re.Pattern[str]:
    verbs = "|".join(re.escape(v) for v in cfg.verbs)
    statuses = "|".join(re.escape(s) for s in (cfg.active_status, cfg.superseded_status))
    return re.compile(
        rf"^- (?P<verb>{verbs}): (?P<text>.+)\n"
        rf"  <!-- observed: (?P<observed>\d{{4}}-\d{{2}}-\d{{2}}) \| status: (?P<status>{statuses})"
        rf" \| scope: (?P<scope>\S+) -->\n?",
        re.MULTILINE,
    )


def format_directive(d: Directive) -> str:
    return f"- {d.verb}: {d.text}\n  <!-- observed: {d.observed} | status: {d.status} | scope: {d.scope} -->\n"


def apply_directive(text: str, new: Directive, cfg: EditLearningConfig) -> str:
    """Insert ``new``. An active directive with the same scope is marked superseded where it stands."""
    pattern = _block_re(cfg)
    if any(
        m.group("scope") == new.scope
        and m.group("status") == cfg.active_status
        and m.group("verb") == new.verb
        and m.group("text") == new.text
        for m in pattern.finditer(text)
    ):
        return text

    def _supersede(match: re.Match[str]) -> str:
        whole = match.group(0)
        if match.group("scope") != new.scope or match.group("status") != cfg.active_status:
            return whole
        # Only the comment's status group changes; the directive text may contain the same words.
        start, end = (i - match.start() for i in match.span("status"))
        return whole[:start] + cfg.superseded_status + whole[end:]

    updated = pattern.sub(_supersede, text)
    block = format_directive(new)
    replaced = [
        m
        for m in pattern.finditer(updated)
        if m.group("scope") == new.scope and m.group("status") == cfg.superseded_status
    ]
    if replaced:
        insert = replaced[-1].end()
        if not updated[:insert].endswith("\n"):
            block = "\n" + block
        return updated[:insert] + block + updated[insert:]
    return _insert_in_section(updated, block, cfg)


def _insert_in_section(text: str, block: str, cfg: EditLearningConfig) -> str:
    """Put ``block`` at the end of the directives section, dropping the empty-marker line."""
    header = cfg.section
    found = re.search(rf"(?m)^{re.escape(header)}[ \t]*$", text)  # the heading line, not the words anywhere
    if found is None:
        tail = "" if not text or text.endswith("\n") else "\n"
        gap = "\n" if text.strip() else ""
        return f"{text}{tail}{gap}{header}\n{block}"
    line_end = text.find("\n", found.start())
    if line_end == -1:
        return text + "\n" + block
    rest_start = line_end + 1
    next_heading = re.search(r"(?m)^## ", text[rest_start:])
    section_end = rest_start + next_heading.start() if next_heading else len(text)
    kept = [ln for ln in text[rest_start:section_end].split("\n") if ln.strip() != cfg.empty_marker]
    while kept and kept[-1] == "":
        kept.pop()
    body = ("\n".join(kept) + "\n") if kept else ""
    return text[:rest_start] + body + block + text[section_end:]
