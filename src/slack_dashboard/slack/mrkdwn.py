import re

_USER_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
_CHANNEL_LINK_RE = re.compile(r"<#[A-Z0-9]+\|([^>]+)>")
_CHANNEL_LINK_NO_LABEL_RE = re.compile(r"<#[A-Z0-9]+>")
_URL_LINK_RE = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
_URL_LINK_NO_LABEL_RE = re.compile(r"<(https?://[^>]+)>")
_EMOJI_RE = re.compile(r":[a-z0-9_+-]+:")
_BOLD_RE = re.compile(r"\*([^*]+)\*")
_ITALIC_RE = re.compile(r"_([^_]+)_")
_CODE_RE = re.compile(r"`([^`]+)`")


def strip_mrkdwn(text: str) -> str:
    if not text:
        return text
    result = _USER_MENTION_RE.sub("@user", text)
    result = _CHANNEL_LINK_RE.sub(r"#\1", result)
    result = _CHANNEL_LINK_NO_LABEL_RE.sub("#channel", result)
    result = _URL_LINK_RE.sub(r"\2", result)
    result = _URL_LINK_NO_LABEL_RE.sub(r"\1", result)
    result = _EMOJI_RE.sub("", result)
    result = _BOLD_RE.sub(r"\1", result)
    result = _ITALIC_RE.sub(r"\1", result)
    result = _CODE_RE.sub(r"\1", result)
    return result
