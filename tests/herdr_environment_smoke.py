#!/usr/bin/env python3
"""Exercise Herdr action and event JSON environments against real worktrees."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT))

import worktree_bootstrap as plugin


def run(*argv: str, cwd: Optional[Path] = None) -> None:
    subprocess.run(argv, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source"
        action_target = root / "action-target"
        event_target = root / "event-target"
        state = root / "state"
        config = root / "plugin-config"
        config.mkdir()
        run("git", "init", "-q", os.fspath(source))
        run("git", "config", "user.name", "Smoke Test", cwd=source)
        run("git", "config", "user.email", "smoke@example.com", cwd=source)
        (source / ".gitignore").write_text(".local-value\n", encoding="utf-8")
        run("git", "add", ".gitignore", cwd=source)
        run("git", "commit", "-qm", "initial", cwd=source)
        run("git", "worktree", "add", "-qb", "action-smoke", os.fspath(action_target), cwd=source)
        run("git", "worktree", "add", "-qb", "event-smoke", os.fspath(event_target), cwd=source)

        control = source / plugin.COPY_LIST
        control.parent.mkdir(parents=True)
        control.write_text(".local-value\n", encoding="utf-8")
        (source / ".local-value").write_text("from action", encoding="utf-8")

        action_env = {
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(
                {
                    "workspace_cwd": os.fspath(action_target),
                    "focused_pane_cwd": os.fspath(action_target),
                }
            ),
            "HERDR_PLUGIN_STATE_DIR": os.fspath(state),
            "HERDR_PLUGIN_CONFIG_DIR": os.fspath(config),
        }
        if plugin.main(["sync"], env=action_env) != 0:
            raise RuntimeError("action-context smoke invocation failed")
        if (action_target / ".local-value").read_text(encoding="utf-8") != "from action":
            raise RuntimeError("action-context target was not synchronized")

        (source / ".local-value").write_text("from event", encoding="utf-8")
        event_env = {
            "HERDR_PLUGIN_EVENT_JSON": json.dumps(
                {"data": {"worktree": {"path": os.fspath(event_target)}}}
            ),
            "HERDR_PLUGIN_STATE_DIR": os.fspath(state),
            "HERDR_PLUGIN_CONFIG_DIR": os.fspath(config),
        }
        if plugin.main(["bootstrap"], env=event_env) != 0:
            raise RuntimeError("event-context smoke invocation failed")
        if (event_target / ".local-value").read_text(encoding="utf-8") != "from event":
            raise RuntimeError("event-context target was not bootstrapped")
        run("git", "worktree", "remove", os.fspath(event_target), cwd=source)
        removed_env = {
            "HERDR_PLUGIN_EVENT": "worktree.removed",
            "HERDR_PLUGIN_CONFIG_DIR": os.fspath(config),
            "HERDR_PLUGIN_EVENT_JSON": json.dumps(
                {
                    "event": "worktree.removed",
                    "data": {
                        "type": "worktree_removed",
                        "workspace_id": "smoke-workspace",
                        "workspace": {
                            "workspace_id": "smoke-workspace",
                            "worktree": {
                                "repo_root": os.fspath(source),
                                "repo_key": os.fspath(source / ".git"),
                                "repo_name": source.name,
                                "checkout_path": os.fspath(event_target),
                                "is_linked_worktree": True,
                            },
                        },
                        "worktree": {
                            "path": os.fspath(event_target),
                            "branch": "event-smoke",
                            "is_bare": False,
                            "is_detached": False,
                            "is_prunable": False,
                            "is_linked_worktree": True,
                            "label": "event-smoke",
                        },
                        "forced": False,
                    },
                }
            ),
        }
        pending = plugin.handle_branch_cleanup_event(removed_env, state, open_popup=False)
        if not plugin.pending_cleanup_path(state, pending["id"]).exists():
            raise RuntimeError("removal event did not retain its pending cleanup")
        review_result = plugin.review_pending_cleanups(
            state,
            input_fn=lambda prompt: "d",
            output_fn=lambda line: None,
        )
        if review_result != 0:
            raise RuntimeError("cleanup review did not complete successfully")
        if plugin.list_pending_cleanups(state):
            raise RuntimeError("completed cleanup left a pending request")
        result = json.loads(
            (state / plugin.BRANCH_CLEANUP_DIR / "last-result.json").read_text(encoding="utf-8")
        )
        if (
            result.get("id") != pending["id"]
            or result.get("branch_outcome") != "deleted_safely"
            or result.get("checkout_outcome") != "absent"
            or result.get("outcome") != "deleted_safely"
        ):
            raise RuntimeError("cleanup persisted an incorrect branch or checkout outcome")
        branch_check = subprocess.run(
            ["git", "-C", os.fspath(source), "show-ref", "--verify", "--quiet", "refs/heads/event-smoke"],
            check=False,
        )
        if branch_check.returncode == 0:
            raise RuntimeError("removed worktree branch was not safely deleted")
    print("Herdr action/event environment smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
