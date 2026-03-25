from slack_dashboard.slack.mrkdwn import strip_mrkdwn


def test_strip_user_mentions() -> None:
    text = "Hey <@U12345> can you look at this?"
    assert strip_mrkdwn(text) == "Hey @user can you look at this?"


def test_strip_channel_links() -> None:
    text = "Check <#C12345|sre-internal> for details"
    assert strip_mrkdwn(text) == "Check #sre-internal for details"


def test_strip_channel_links_no_label() -> None:
    text = "Check <#C12345> for details"
    assert strip_mrkdwn(text) == "Check #channel for details"


def test_strip_url_links() -> None:
    text = "See <https://example.com|example docs>"
    assert strip_mrkdwn(text) == "See example docs"


def test_strip_url_links_no_label() -> None:
    text = "See <https://example.com>"
    assert strip_mrkdwn(text) == "See https://example.com"


def test_strip_emoji() -> None:
    text = "Great job :thumbsup: :fire:"
    assert strip_mrkdwn(text) == "Great job  "


def test_strip_bold_italic() -> None:
    text = "*bold* and _italic_ text"
    assert strip_mrkdwn(text) == "bold and italic text"


def test_strip_code_blocks() -> None:
    text = "Run `kubectl get pods` please"
    assert strip_mrkdwn(text) == "Run kubectl get pods please"


def test_combined() -> None:
    text = "*Hey* <@U12345>, check <#C99|ops> for :fire: details: <https://x.com|link>"
    result = strip_mrkdwn(text)
    assert "@user" in result
    assert "#ops" in result
    assert "link" in result
    assert "<" not in result
    assert ">" not in result
    assert ":" not in result or result.count(":") == 1  # only the colon after "details"


def test_empty_string() -> None:
    assert strip_mrkdwn("") == ""


def test_plain_text_unchanged() -> None:
    text = "Just a normal message with no formatting"
    assert strip_mrkdwn(text) == text
