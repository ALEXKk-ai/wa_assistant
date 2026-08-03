from app.workflows.owner import parse_owner_command


def test_parses_takeover():
    cmd = parse_owner_command("TAKEOVER 254712345678")
    assert cmd.name == "TAKEOVER"
    assert cmd.args == ["254712345678"]


def test_parses_release_case_insensitive():
    cmd = parse_owner_command("release 254712345678")
    assert cmd.name == "RELEASE"


def test_parses_reply_with_multiword_message():
    cmd = parse_owner_command("REPLY 254712345678 On my way, thanks for waiting")
    assert cmd.name == "REPLY"
    assert cmd.args == ["254712345678", "On my way, thanks for waiting"]


def test_parses_confirm_ref():
    cmd = parse_owner_command("CONFIRM B12")
    assert cmd.name == "CONFIRM"
    assert cmd.args == ["B12"]


def test_parses_reject_ref():
    cmd = parse_owner_command("reject O5")
    assert cmd.name == "REJECT"
    assert cmd.args == ["O5"]


def test_unrecognized_text_is_unknown():
    cmd = parse_owner_command("hey what's up")
    assert cmd.name == "UNKNOWN"


def test_empty_text_is_unknown():
    cmd = parse_owner_command("   ")
    assert cmd.name == "UNKNOWN"
    assert cmd.args == []
