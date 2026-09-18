"""The GitHub Action: its comment logic, and the shape of action.yml.

The comment script is tested against an in-memory stand-in for the GitHub API.
`action.yml` itself can only run on GitHub, so what is checked here are the
properties that matter and are easy to break: every input is wired through, no
input is interpolated into a shell script, and every action it uses is pinned.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

import upsert_comment

ROOT = Path(__file__).resolve().parents[2]
PLAN_MARKER = "<!-- deltaplan:plan:prod -->"


class FakeGitHub:
    """Just enough of the issue-comments API."""

    def __init__(self, bodies: list[str] | None = None) -> None:
        self.comments: list[dict[str, Any]] = [
            {"id": 100 + index, "body": body} for index, body in enumerate(bodies or [])
        ]
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, path: str, payload: dict[str, Any] | None) -> Any:
        self.calls.append((method, path))
        if method == "GET":
            found = re.search(r"[?&]page=(\d+)", path)  # not the `page` in per_page
            assert found is not None
            page = int(found.group(1))
            size = upsert_comment.PAGE_SIZE
            return self.comments[(page - 1) * size : page * size]
        assert payload is not None
        if method == "POST":
            self.comments.append({"id": 900, "body": payload["body"]})
            return self.comments[-1]
        comment_id = int(path.rsplit("/", 1)[1])
        for comment in self.comments:
            if comment["id"] == comment_id:
                comment["body"] = payload["body"]
                return comment
        raise AssertionError(f"no comment {comment_id}")


def body(marker: str = PLAN_MARKER, text: str = "the plan") -> str:
    return f"{marker}\n### deltaplan plan\n{text}\n"


def test_the_first_run_creates_a_comment() -> None:
    github = FakeGitHub(["an unrelated comment"])
    assert upsert_comment.upsert(github, "o/r", "7", body()) == "created"
    assert github.comments[-1]["body"] == body()
    assert github.calls[-1] == ("POST", "/repos/o/r/issues/7/comments")


def test_a_later_run_updates_it_in_place() -> None:
    github = FakeGitHub(["hello", body(text="old plan")])
    result = upsert_comment.upsert(github, "o/r", "7", body(text="new plan"))
    assert result == "updated comment 101"
    assert github.comments[1]["body"] == body(text="new plan")
    assert len(github.comments) == 2, "no second comment"


def test_a_quoted_comment_is_not_mistaken_for_ours() -> None:
    # A reply that quotes deltaplan carries the marker, but not at the start.
    quoted = "> " + body().replace("\n", "\n> ") + "\nlooks risky?"
    github = FakeGitHub([quoted])
    assert upsert_comment.upsert(github, "o/r", "7", body()) == "created"
    assert github.comments[0]["body"] == quoted, "the reply is left alone"


def test_targets_and_commands_keep_their_own_comments() -> None:
    drift = "<!-- deltaplan:drift:prod -->"
    dev = "<!-- deltaplan:plan:dev -->"
    github = FakeGitHub([body(drift, "drift"), body(dev, "dev plan")])
    assert upsert_comment.upsert(github, "o/r", "7", body()) == "created"
    assert github.comments[0]["body"] == body(drift, "drift")
    assert github.comments[1]["body"] == body(dev, "dev plan")


def test_it_looks_past_the_first_page() -> None:
    filler = ["noise"] * upsert_comment.PAGE_SIZE
    github = FakeGitHub([*filler, body(text="old")])
    result = upsert_comment.upsert(github, "o/r", "7", body(text="new"))
    assert result == f"updated comment {100 + upsert_comment.PAGE_SIZE}"
    assert ("GET", "/repos/o/r/issues/7/comments?per_page=100&page=2") in github.calls


def test_a_body_without_a_marker_is_refused() -> None:
    with pytest.raises(ValueError, match="must start with a deltaplan marker"):
        upsert_comment.upsert(FakeGitHub(), "o/r", "7", "### a plan\n")


def test_the_markdown_renderer_writes_the_marker_the_script_reads() -> None:
    # The two halves agree on the marker, or the comment would never be found.
    from deltaplan.render.markdown import marker

    assert marker("plan", "prod") == PLAN_MARKER
    assert upsert_comment.marker_of(body(marker("plan", "prod"))) == PLAN_MARKER


# ---------------------------------------------------------------------------
# action.yml
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def action() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "action.yml").read_text())


def test_it_is_a_composite_action(action: dict[str, Any]) -> None:
    assert action["runs"]["using"] == "composite"
    assert set(action["inputs"]) == {
        "command",
        "target",
        "config",
        "working-directory",
        "clone",
        "comment",
        "fail-on-drift",
        "github-token",
    }
    assert action["inputs"]["target"]["required"] is True
    assert set(action["outputs"]) == {"has-changes", "plan-file", "markdown-file"}


def test_every_input_is_used(action: dict[str, Any]) -> None:
    text = (ROOT / "action.yml").read_text()
    for name in action["inputs"]:
        assert f"inputs.{name}" in text, f"input {name!r} is declared but never used"


def test_no_input_is_interpolated_into_a_script(action: dict[str, Any]) -> None:
    """`${{ inputs.x }}` inside `run:` is a script-injection hole; use env instead.

    https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions#understanding-the-risk-of-script-injections
    """
    for step in action["runs"]["steps"]:
        script = step.get("run", "")
        assert "${{" not in script, f"step {step.get('id', step)} interpolates into run:"


def test_every_action_used_is_pinned(action: dict[str, Any]) -> None:
    for step in action["runs"]["steps"]:
        if "uses" in step:
            assert re.search(r"@v\d+|@[0-9a-f]{40}$", step["uses"]), step["uses"]


def test_every_run_step_names_its_shell(action: dict[str, Any]) -> None:
    # Composite actions require it.
    for step in action["runs"]["steps"]:
        if "run" in step:
            assert step.get("shell") == "bash"


def test_the_comment_script_is_where_the_action_looks_for_it(
    action: dict[str, Any],
) -> None:
    text = (ROOT / "action.yml").read_text()
    assert '"$ACTION_PATH/action/upsert_comment.py"' in text
    assert (ROOT / "action" / "upsert_comment.py").is_file()
