import re
from collections.abc import Mapping

# <@U123> or <@U123|label>; group 1 is the user id, group 2 the optional label.
_USER_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|([^>]+))?>")
_CHANNEL_LINK_RE = re.compile(r"<#[A-Z0-9]+\|([^>]+)>")
_CHANNEL_LINK_NO_LABEL_RE = re.compile(r"<#[A-Z0-9]+>")
_URL_LINK_RE = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
_URL_LINK_NO_LABEL_RE = re.compile(r"<(https?://[^>]+)>")
# Slack "special" mentions: <!subteam^S123|@handle>, <!subteam^S123>, <!here>, <!channel>,
# <!everyone>, <!date^ts^format|fallback>. Slack delivers a usergroup mention WITHOUT its
# |@handle label on most payloads, so the bare form must render as something a human reads
# (@group) rather than the wire token leaking into titles (seen live, 2026-10-17).
_SPECIAL_LABEL_RE = re.compile(r"<!([a-z]+)(?:\^[^|>]*)?\|([^>]+)>")
_SUBTEAM_NO_LABEL_RE = re.compile(r"<!subteam\^[A-Z0-9]+>")
_SPECIAL_NO_LABEL_RE = re.compile(r"<!([a-z]+)(?:\^[^>]*)?>")


_MENTION_KINDS = frozenset({"subteam", "here", "channel", "everyone"})


def _special_label(match: re.Match[str]) -> str:
    kind, label = match.group(1), match.group(2).strip()
    if kind in _MENTION_KINDS and not label.startswith("@"):
        return f"@{label}"
    return label


_EMOJI_RE = re.compile(r":[a-z0-9_+-]+:")
_BOLD_RE = re.compile(r"\*([^*]+)\*")
_ITALIC_RE = re.compile(r"_([^_]+)_")
_CODE_RE = re.compile(r"`([^`]+)`")


def mention_ids(text: str) -> list[str]:
    """User ids mentioned as ``<@U...>`` in ``text``, in order of first appearance."""
    return list(dict.fromkeys(m.group(1) for m in _USER_MENTION_RE.finditer(text or "")))


def strip_mrkdwn(text: str, names: Mapping[str, str] | None = None) -> str:
    """Plain text from Slack mrkdwn. ``names`` maps a user id to the name a mention renders as;
    an id missing from it falls back to the mention's own label, then to ``@user``."""
    if not text:
        return text
    known = names or {}

    def _user_label(match: re.Match[str]) -> str:
        name = known.get(match.group(1)) or match.group(2)
        return f"@{name}" if name else "@user"

    result = _USER_MENTION_RE.sub(_user_label, text)
    result = _CHANNEL_LINK_RE.sub(r"#\1", result)
    result = _CHANNEL_LINK_NO_LABEL_RE.sub("#channel", result)
    result = _URL_LINK_RE.sub(r"\2", result)
    result = _URL_LINK_NO_LABEL_RE.sub(r"\1", result)
    result = _SPECIAL_LABEL_RE.sub(_special_label, result)
    result = _SUBTEAM_NO_LABEL_RE.sub("@group", result)
    result = _SPECIAL_NO_LABEL_RE.sub(r"@\1", result)
    result = _EMOJI_RE.sub("", result)
    result = _BOLD_RE.sub(r"\1", result)
    result = _ITALIC_RE.sub(r"\1", result)
    result = _CODE_RE.sub(r"\1", result)
    return result
