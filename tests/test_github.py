from symphony.github import STATUS_MARKER, GhCLIBackend


def test_canonical_status_comment_must_be_owned_by_authenticated_bot(monkeypatch):
    backend = GhCLIBackend(("solo/project",), bot_login="symphony-bot")
    comments = [
        {"id": 1, "body": STATUS_MARKER, "user": {"login": "attacker"}},
        {"id": 2, "body": STATUS_MARKER, "user": {"login": "symphony-bot"}},
    ]

    def fake_run(args, *, json_output=False):
        assert json_output
        return comments

    monkeypatch.setattr(backend, "_run", fake_run)
    assert backend._find_status_comment("solo/project", 7) == 2
