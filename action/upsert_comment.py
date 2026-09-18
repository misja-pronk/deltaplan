#!/usr/bin/env python3
"""Post deltaplan's comment on a pull request, or update the one it posted before.

Used by the GitHub Action (`action.yml`). Standard library only, so it runs on
any runner with a Python — no `gh`, no `jq`, no dependencies to install.

The comment is found by the hidden marker on its first line
(`<!-- deltaplan:plan:prod -->`), which the Markdown renderer writes. One marker
per command and target means a `plan` and a `drift` comment, or comments for two
targets, never overwrite each other.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

MARKER_PREFIX = "<!-- deltaplan:"
PAGE_SIZE = 100

#: (method, path, payload) -> decoded JSON. A seam, so tests need no network.
Api = Callable[[str, str, dict[str, Any] | None], Any]


def marker_of(body: str) -> str:
    """The marker the body starts with. Without one there is nothing to update."""
    first = body.splitlines()[0] if body else ""
    if not first.startswith(MARKER_PREFIX):
        raise ValueError(
            "the comment body must start with a deltaplan marker — render it with "
            "`deltaplan plan -f md` or `deltaplan show -f md`"
        )
    return first


def find_comment(api: Api, repo: str, pr: str, marker: str) -> int | None:
    """The id of the comment deltaplan posted for this marker, if any.

    Only a comment that *starts* with the marker counts. Someone quoting
    deltaplan's comment in a reply carries the marker along, but not at the start.
    """
    page = 1
    while True:
        comments = api(
            "GET",
            f"/repos/{repo}/issues/{pr}/comments?per_page={PAGE_SIZE}&page={page}",
            None,
        )
        for comment in comments:
            if (comment.get("body") or "").startswith(marker):
                return int(comment["id"])
        if len(comments) < PAGE_SIZE:
            return None
        page += 1


def upsert(api: Api, repo: str, pr: str, body: str) -> str:
    """Create or update the comment. Returns what it did."""
    existing = find_comment(api, repo, pr, marker_of(body))
    if existing is None:
        api("POST", f"/repos/{repo}/issues/{pr}/comments", {"body": body})
        return "created"
    api("PATCH", f"/repos/{repo}/issues/comments/{existing}", {"body": body})
    return f"updated comment {existing}"


def github(token: str, base: str = "https://api.github.com") -> Api:
    """The real API, over urllib."""

    def call(method: str, path: str, payload: dict[str, Any] | None) -> Any:
        request = urllib.request.Request(
            base.rstrip("/") + path,
            method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    return call


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: upsert_comment.py BODY_FILE", file=sys.stderr)
        return 2
    body = Path(argv[1]).read_text(encoding="utf-8")
    api = github(
        os.environ["GITHUB_TOKEN"],
        # GitHub Enterprise Server sets its own API URL.
        os.environ.get("GITHUB_API_URL", "https://api.github.com"),
    )
    print(upsert(api, os.environ["GITHUB_REPOSITORY"], os.environ["PR_NUMBER"], body))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
