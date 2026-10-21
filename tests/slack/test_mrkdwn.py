from slack_dashboard.slack.mrkdwn import mention_ids, strip_mrkdwn


def test_strip_user_mentions() -> None:
    text = "Hey <@U12345> can you look at this?"
    assert strip_mrkdwn(text) == "Hey @user can you look at this?"


def test_user_mentions_render_resolved_names() -> None:
    # A hover quote reading "thoughts @user or @user?" told the reader nothing (2026-10-20).
    text = "thoughts <@U1> or <@U2>?"
    assert strip_mrkdwn(text, {"U1": "jess", "U2": "tonyo"}) == "thoughts @jess or @tonyo?"


def test_unresolved_mention_falls_back_to_label_then_user() -> None:
    text = "<@U1|keegan> and <@U2>"
    assert strip_mrkdwn(text, {}) == "@keegan and @user"
    assert strip_mrkdwn(text, {"U1": "keegan.r"}) == "@keegan.r and @user"


def test_mention_ids_in_order_deduped() -> None:
    assert mention_ids("<@U2> <@U1|x> <@U2> <!subteam^S1>") == ["U2", "U1"]
    assert mention_ids("") == []


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


def test_strip_usergroup_mention_no_label() -> None:
    # The form Slack delivers on most payloads; leaked into live titles on 2026-10-17.
    text = "impromptu datahub meeting now <!subteam^S02DL6LHZAQ>? anyone"
    assert strip_mrkdwn(text) == "impromptu datahub meeting now @group? anyone"


def test_strip_usergroup_mention_with_label() -> None:
    text = "ping <!subteam^S02DL6LHZAQ|@datahub> please"
    assert strip_mrkdwn(text) == "ping @datahub please"


def test_strip_here_channel_everyone() -> None:
    assert strip_mrkdwn("<!here> deploy starting") == "@here deploy starting"
    assert strip_mrkdwn("<!channel> heads up") == "@channel heads up"
    assert strip_mrkdwn("<!everyone> all hands") == "@everyone all hands"


def test_strip_date_token_uses_fallback_label() -> None:
    text = "due <!date^1392734382^{date_short}|Feb 18, 2014>"
    assert strip_mrkdwn(text) == "due Feb 18, 2014"


def test_no_wire_token_survives() -> None:
    text = "<@U1> <#C1|ops> <!subteam^S1> <!here> <https://x.com|x> <!date^1^{date}|d>"
    result = strip_mrkdwn(text)
    assert "<" not in result and ">" not in result


def test_empty_string() -> None:
    assert strip_mrkdwn("") == ""


def test_plain_text_unchanged() -> None:
    text = "Just a normal message with no formatting"
    assert strip_mrkdwn(text) == text
