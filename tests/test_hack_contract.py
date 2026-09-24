import pytest

from symphony.hack_contract import HackContractError, allowed_paths, parse_board, parse_lanes, validate_footprint


def test_lanes_and_board_preserve_priority_identity_dependencies_and_checkboxes():
    lanes = parse_lanes(b'[lanes.api]\npaths=["server/**"]\n[lanes.ui]\npaths=["app/**"]\n')
    tasks = parse_board(
        "# Campaign\n## api\n- [ ] [contract] Define API\n## ui\n"
        "- [ ] [screen] Build screen <!-- depends: contract -->\n- [x] Already done\n", lanes,
    )
    assert [(task.key, task.lane, task.priority) for task in tasks[:2]] == [("contract", "api", 0), ("screen", "ui", 1)]
    assert tasks[1].depends_on == ("contract",)
    assert tasks[2].checked
    assert parse_board("## api\n- [ ] Same prompt", lanes)[0].key == parse_board("# Changed heading\n## api\n- [ ] Same prompt", lanes)[0].key


@pytest.mark.parametrize("board", [
    "- [ ] Outside lane", "## unknown\n- [ ] Task", "## api\n- [ ] [a] First\n- [ ] [a] Duplicate",
    "## api\n- [ ] [a] Cycle <!-- depends: a -->", "## api\n- [ ] [a] Unknown <!-- depends: missing -->",
    "## api\n- [ ] [a] First <!-- depends: b -->\n- [ ] [b] Second <!-- depends: a -->",
])
def test_invalid_boards_are_rejected_atomically(board):
    with pytest.raises(HackContractError):
        parse_board(board, {"api": ["server/**"]})


@pytest.mark.parametrize("lanes", [
    {"api": ["src/**"], "ui": ["src/ui/**"]},
    {"api": ["**/*.py"], "ui": ["app/main.py"]},
    {"api": ["../outside"]}, {"api": ["/etc/passwd"]}, {"api": [".git/**"]},
    {"scaffold": ["src/**"]}, {"dispatcher": ["src/**"]}, {"api": []},
])
def test_unsafe_or_overlapping_lanes_rejected(lanes):
    with pytest.raises(HackContractError):
        parse_lanes(lanes)


def test_glob_footprints_enforce_root_boundaries_and_recursive_files():
    assert parse_lanes({"api": ["server/**"], "ui": ["app/**"], "infra": ["*.toml"]})
    assert allowed_paths(["app/main.py", "app/deep/nested.py"], ["app/**"])
    assert allowed_paths(["main.py", "app/main.py"], ["**/*.py"])
    assert allowed_paths(["pyproject.toml"], ["*.toml"])
    assert not allowed_paths(["app/pyproject.toml"], ["*.toml"])
    assert not allowed_paths(["app/main.py", "server/main.py"], ["app/**"])
    assert not allowed_paths([".git/config"], ["**"])
    assert not allowed_paths(["app/../server/main.py"], ["app/**"])
    with pytest.raises(HackContractError, match="server/main.py"):
        validate_footprint(["app/main.py", "server/main.py"], ["app/**"])
