"""The onboarding beat marker is an empty id token, not part of the reply.

The model may end a reply with ``<onboarding-beat id="..."/>``. That token is how we know the beat was
offered. A tag with a body is the model pasting its private guidance: it is dropped, and it does not count
as an offer. Nothing inside either form is shown to the user.
"""

from __future__ import annotations

import re

_OPEN = "<onboarding-beat"
# Empty marker only. A tag with a body is not an offer.
_SELF = re.compile(r'<onboarding-beat\s+id="([^"]+)"\s*/>')
# Self-closing, or an element with any body, including the instruction text the model should not have copied.
_ANY = re.compile(
    r"<onboarding-beat\b[^>]*/>|<onboarding-beat\b[^>]*>.*?</onboarding-beat>",
    re.DOTALL,
)


def offered_ids(text: str) -> list[str]:
    """Beat ids named by empty markers. A body, or an unclosed tag, is not an offer."""
    if _unclosed(text):
        return []
    return _SELF.findall(text)


def visible_reply(text: str) -> str:
    """The reply with every beat tag removed. An unclosed tag takes the rest of the reply with it."""
    cleaned = _ANY.sub("", text)
    open_at = cleaned.find(_OPEN)
    if open_at >= 0:
        cleaned = cleaned[:open_at]
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _unclosed(text: str) -> bool:
    """True when an opening beat tag is still there after every complete tag is removed."""
    return _OPEN in _ANY.sub("", text)


class ReplyScrubber:
    """Streaming form of ``visible_reply``. Holds a partial tag so its body is never emitted."""

    def __init__(self) -> None:
        self._pending = ""

    def push(self, piece: str) -> str:
        self._pending += piece
        out: list[str] = []
        while self._pending:
            start = self._pending.find("<")
            if start < 0:
                out.append(self._pending)
                self._pending = ""
                break
            out.append(self._pending[:start])
            rest = self._pending[start:]
            found = _ANY.match(rest)
            if found:
                self._pending = rest[found.end() :]
                continue
            if _might_be_tag(rest):
                self._pending = rest
                break
            out.append("<")
            self._pending = rest[1:]
        return "".join(out)

    def finish(self) -> str:
        """Flush text that was not a beat tag. An unclosed beat tag is dropped."""
        pending = self._pending
        self._pending = ""
        if pending.startswith(_OPEN):
            return ""
        return pending


def _might_be_tag(rest: str) -> bool:
    """True while ``rest`` could still grow into a beat tag. A finished tag is handled by ``_ANY``."""
    if _OPEN.startswith(rest) and not rest.startswith(_OPEN):
        return True
    if not rest.startswith(_OPEN):
        return False
    return not (_SELF.match(rest) or "</onboarding-beat>" in rest)
