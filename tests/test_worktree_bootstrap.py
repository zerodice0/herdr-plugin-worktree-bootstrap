from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(ROOT))

import worktree_bootstrap as plugin


def run(command, *, cwd=None, check=True):
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


class PathValidationTests(unittest.TestCase):
    def test_copy_list_supports_comments_spaces_unicode_crlf_and_dot_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control = root / plugin.COPY_LIST
            control.parent.mkdir()
            control.write_bytes(
                b"# comment\r\n\r\nspace dir/file.txt\r\n"
                + "한글/설정.json\r\nfoo..bar\r\n".encode("utf-8")
            )
            self.assertEqual(
                plugin.read_copy_list(root),
                ["space dir/file.txt", "한글/설정.json", "foo..bar"],
            )

    def test_rejects_unsafe_or_ambiguous_paths(self):
        invalid = [".", "/absolute", "../escape", "a/../b", "a/./b", "a//b", "a/"]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(plugin.BootstrapError):
                plugin.validate_relative_path(value)

    def test_rejects_duplicate_and_overlapping_paths(self):
        cases = [
            ["cache", "cache"],
            ["cache", "cache/npm"],
            ["cache/npm", "cache"],
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(plugin.BootstrapError):
                plugin.validate_copy_paths(values)

    def test_foo_dot_dot_bar_is_valid(self):
        self.assertEqual(plugin.validate_relative_path("foo..bar"), "foo..bar")


class ContextResolverTests(unittest.TestCase):
    def test_explicit_target_wins(self):
        env = {
            "HERDR_PLUGIN_EVENT_JSON": json.dumps({"data": {"worktree": {"path": "/event"}}}),
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace": {"cwd": "/context"}}),
        }
        self.assertEqual(plugin.resolve_target_path("/explicit", env), Path("/explicit"))

    def test_event_worktree_path_wins_over_action_context(self):
        env = {
            "HERDR_PLUGIN_EVENT_JSON": json.dumps({"data": {"worktree": {"path": "/event"}}}),
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace": {"cwd": "/context"}}),
        }
        self.assertEqual(plugin.resolve_target_path(None, env), Path("/event"))

    def test_action_uses_workspace_cwd(self):
        env = {"HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"workspace": {"cwd": "/context"}})}
        self.assertEqual(plugin.resolve_target_path(None, env), Path("/context"))

    def test_action_uses_flat_herdr_075_workspace_cwd(self):
        env = {
            "HERDR_PLUGIN_CONTEXT_JSON": json.dumps(
                {
                    "workspace_cwd": "/actual-herdr-context",
                    "focused_pane_cwd": "/focused-pane",
                }
            )
        }
        self.assertEqual(plugin.resolve_target_path(None, env), Path("/actual-herdr-context"))

    def test_invalid_environment_json_is_reported(self):
        with self.assertRaises(plugin.BootstrapError):
            plugin.resolve_target_path(None, {"HERDR_PLUGIN_CONTEXT_JSON": "{"})


class GitRepositoryTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "primary checkout"
        self.target = self.root / "feature checkout"
        self.state_dir = self.root / "state"
        run(["git", "init", "-q", os.fspath(self.source)])
        run(["git", "config", "user.name", "Test User"], cwd=self.source)
        run(["git", "config", "user.email", "test@example.com"], cwd=self.source)
        (self.source / ".gitignore").write_text(
            ".env\ncache/\nspace dir/\nunicode-한글/\nlinks/\nexternal-link\nnested/\n.a\n.b\n",
            encoding="utf-8",
        )
        (self.source / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        run(["git", "add", ".gitignore", "tracked.txt"], cwd=self.source)
        run(["git", "commit", "-qm", "initial"], cwd=self.source)
        run(["git", "worktree", "add", "-qb", "feature", os.fspath(self.target)], cwd=self.source)
        self.context = plugin.resolve_repository(self.target)

    def tearDown(self):
        self.temporary.cleanup()

    def write_copy_list(self, paths):
        control = self.source / plugin.COPY_LIST
        control.parent.mkdir(parents=True, exist_ok=True)
        control.write_text("\n".join(paths) + "\n", encoding="utf-8")

    def write_setup(self, commands):
        control = self.source / plugin.SETUP_FILE
        control.parent.mkdir(parents=True, exist_ok=True)
        control.write_text(
            json.dumps({"version": 1, "commands": commands}, ensure_ascii=False),
            encoding="utf-8",
        )


class RepositoryResolutionTests(GitRepositoryTestCase):
    def test_finds_primary_checkout_from_linked_worktree(self):
        self.assertEqual(self.context.source, self.source.resolve())
        self.assertEqual(self.context.target, self.target.resolve())
        self.assertFalse(self.context.target_is_primary)

    def test_primary_checkout_action_is_a_successful_noop(self):
        primary = plugin.resolve_repository(self.source)
        self.assertTrue(primary.target_is_primary)
        with self.assertRaises(plugin.NothingToDo):
            plugin.execute_action("sync", primary, self.state_dir)

    def test_bare_repository_is_a_successful_noop(self):
        bare = self.root / "bare.git"
        run(["git", "init", "-q", "--bare", os.fspath(bare)])
        with self.assertRaises(plugin.NothingToDo):
            plugin.resolve_repository(bare)

    def test_missing_primary_checkout_is_a_successful_noop(self):
        bare = self.root / "origin.git"
        linked = self.root / "bare-linked"
        run(["git", "clone", "-q", "--bare", os.fspath(self.source), os.fspath(bare)])
        run(["git", "--git-dir", os.fspath(bare), "worktree", "add", "--detach", os.fspath(linked)])
        with self.assertRaises(plugin.NothingToDo):
            plugin.resolve_repository(linked)

    def test_no_configuration_is_a_successful_noop(self):
        with self.assertRaises(plugin.NothingToDo):
            plugin.execute_action("bootstrap", self.context, self.state_dir)


class CopyBehaviorTests(GitRepositoryTestCase):
    def test_copies_files_directories_modes_spaces_unicode_and_is_idempotent(self):
        (self.source / ".env").write_bytes(b"SECRET=value\n")
        os.chmod(self.source / ".env", 0o600)
        cache = self.source / "cache"
        cache.mkdir()
        executable = cache / "tool"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(executable, 0o755)
        (self.source / "space dir").mkdir()
        (self.source / "space dir" / "a file").write_text("space", encoding="utf-8")
        (self.source / "unicode-한글").mkdir()
        (self.source / "unicode-한글" / "설정").write_text("값", encoding="utf-8")
        self.write_copy_list([".env", "cache", "space dir", "unicode-한글"])

        result = plugin.execute_action("sync", self.context, self.state_dir)
        self.assertEqual(result["copied_count"], 4)
        self.assertEqual((self.target / ".env").read_bytes(), b"SECRET=value\n")
        self.assertEqual(stat.S_IMODE((self.target / ".env").stat().st_mode), 0o600)
        self.assertTrue((self.target / "cache" / "tool").stat().st_mode & stat.S_IXUSR)
        self.assertEqual((self.target / "space dir" / "a file").read_text(), "space")
        self.assertEqual((self.target / "unicode-한글" / "설정").read_text(), "값")

        second = plugin.execute_action("sync", self.context, self.state_dir)
        self.assertEqual(second["copied_count"], 4)
        self.assertEqual((self.target / ".env").read_bytes(), b"SECRET=value\n")

    def test_directory_replacement_removes_target_only_stale_files(self):
        (self.source / "cache").mkdir()
        (self.source / "cache" / "current").write_text("new", encoding="utf-8")
        (self.target / "cache").mkdir()
        (self.target / "cache" / "stale").write_text("old", encoding="utf-8")
        self.write_copy_list(["cache"])
        plugin.execute_action("sync", self.context, self.state_dir)
        self.assertFalse((self.target / "cache" / "stale").exists())
        self.assertEqual((self.target / "cache" / "current").read_text(), "new")

    def test_tracked_and_unignored_paths_are_skipped_with_reasons(self):
        (self.source / "plain.local").write_text("local", encoding="utf-8")
        self.write_copy_list(["tracked.txt", "plain.local"])
        statuses = plugin.classify_copy_entries(
            self.source, self.target, plugin.read_copy_list(self.source)
        )
        self.assertEqual([item.eligibility for item in statuses], ["tracked", "unignored"])
        result = plugin.execute_action("sync", self.context, self.state_dir)
        self.assertEqual(result["copied_count"], 0)
        self.assertEqual(result["skipped"], {"tracked": 1, "unignored": 1})

    def test_missing_ignored_source_is_skipped(self):
        self.write_copy_list([".env"])
        result = plugin.execute_action("sync", self.context, self.state_dir)
        self.assertEqual(result["copied_count"], 0)
        self.assertEqual(result["skipped"], {"missing": 1})

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_broken_and_external_symlinks_are_copied_without_dereferencing(self):
        outside = self.root / "outside-secret"
        outside.write_text("do not copy", encoding="utf-8")
        links = self.source / "links"
        links.mkdir()
        os.symlink("missing-target", links / "broken")
        os.symlink(os.fspath(outside), links / "external")
        self.write_copy_list(["links"])
        plugin.execute_action("sync", self.context, self.state_dir)
        self.assertTrue((self.target / "links" / "broken").is_symlink())
        self.assertEqual(os.readlink(self.target / "links" / "broken"), "missing-target")
        self.assertTrue((self.target / "links" / "external").is_symlink())
        self.assertEqual(os.readlink(self.target / "links" / "external"), os.fspath(outside))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_target_parent_symlink_cannot_write_outside_worktree(self):
        (self.source / "nested").mkdir()
        (self.source / "nested" / "file").write_text("new", encoding="utf-8")
        outside = self.root / "outside-dir"
        outside.mkdir()
        (outside / "file").write_text("outside", encoding="utf-8")
        os.symlink(os.fspath(outside), self.target / "nested")
        self.write_copy_list(["nested/file"])
        with self.assertRaises(plugin.BootstrapError):
            plugin.execute_action("sync", self.context, self.state_dir)
        self.assertEqual((outside / "file").read_text(), "outside")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_source_parent_symlink_is_rejected(self):
        actual = self.source / "links"
        actual.mkdir()
        (actual / "file").write_text("secret", encoding="utf-8")
        os.symlink("links", self.source / "external-link")
        self.write_copy_list(["external-link/file"])
        with self.assertRaises(plugin.BootstrapError):
            plugin.execute_action("sync", self.context, self.state_dir)

    def test_commit_failure_rolls_back_all_replacements(self):
        for name in (".a", ".b"):
            (self.source / name).write_text(f"new-{name}", encoding="utf-8")
            (self.target / name).write_text(f"old-{name}", encoding="utf-8")
        self.write_copy_list([".a", ".b"])
        statuses = plugin.classify_copy_entries(self.source, self.target, [".a", ".b"])
        real_rename = plugin.os.rename

        def failing_rename(source, destination):
            source_path = Path(source)
            destination_path = Path(destination)
            if source_path.parent.name == "stage" and source_path.name == "1" and destination_path.name == ".b":
                raise OSError("injected commit failure")
            return real_rename(source, destination)

        with mock.patch.object(plugin.os, "rename", side_effect=failing_rename):
            with self.assertRaises(plugin.BootstrapError):
                plugin.sync_paths(self.context, statuses)
        self.assertEqual((self.target / ".a").read_text(), "old-.a")
        self.assertEqual((self.target / ".b").read_text(), "old-.b")

    def test_interruption_cleans_transaction_directory(self):
        (self.source / ".env").write_text("new", encoding="utf-8")
        self.write_copy_list([".env"])
        statuses = plugin.classify_copy_entries(self.source, self.target, [".env"])
        with mock.patch.object(plugin, "_copy_object", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                plugin.sync_paths(self.context, statuses)
        leftovers = [path for path in self.target.iterdir() if path.name.startswith(plugin.TXN_PREFIX)]
        self.assertEqual(leftovers, [])

    def test_stale_transaction_is_removed_before_sync(self):
        stale = self.target / f"{plugin.TXN_PREFIX}stale"
        stale.mkdir()
        (stale / "artifact").write_text("x", encoding="utf-8")
        (self.source / ".env").write_text("new", encoding="utf-8")
        self.write_copy_list([".env"])
        plugin.execute_action("sync", self.context, self.state_dir)
        self.assertFalse(stale.exists())


class SetupBehaviorTests(GitRepositoryTestCase):
    def command(self, name, code, timeout=5):
        return {
            "name": name,
            "argv": [sys.executable, "-c", code],
            "timeout_seconds": timeout,
        }

    def test_commands_run_sequentially_in_target_root(self):
        first = "from pathlib import Path; Path('order').write_text('1')"
        second = "from pathlib import Path; p=Path('order'); p.write_text(p.read_text()+'2')"
        self.write_setup([self.command("first", first), self.command("second", second)])
        result = plugin.execute_action("setup", self.context, self.state_dir)
        self.assertEqual(result["setup_completed_count"], 2)
        self.assertEqual((self.target / "order").read_text(), "12")
        self.assertFalse((self.source / "order").exists())

    def test_first_failure_stops_later_commands_and_reports_exit_code(self):
        later = "from pathlib import Path; Path('should-not-exist').write_text('x')"
        self.write_setup(
            [
                self.command("fail", "raise SystemExit(7)"),
                self.command("later", later),
            ]
        )
        with self.assertRaisesRegex(plugin.BootstrapError, "exit code 7"):
            plugin.execute_action("setup", self.context, self.state_dir)
        self.assertFalse((self.target / "should-not-exist").exists())
        state = plugin.read_run_state(self.state_dir, self.context)
        self.assertEqual(state["failed_command"], {"index": 1, "name": "fail"})
        self.assertEqual(state["failure_kind"], "nonzero_exit")
        self.assertEqual(state["exit_code"], 7)

    def test_timeout_stops_command(self):
        self.write_setup([self.command("slow", "import time; time.sleep(10)", timeout=1)])
        with self.assertRaisesRegex(plugin.BootstrapError, "timed out"):
            plugin.execute_action("setup", self.context, self.state_dir)

    def test_command_not_found_is_reported(self):
        self.write_setup(
            [{"name": "missing", "argv": ["definitely-not-a-real-command-123"], "timeout_seconds": 5}]
        )
        with self.assertRaisesRegex(plugin.BootstrapError, "executable not found"):
            plugin.execute_action("setup", self.context, self.state_dir)

    def test_string_shell_command_is_rejected(self):
        self.write_setup([{"name": "bad", "argv": "echo nope", "timeout_seconds": 5}])
        with self.assertRaises(plugin.BootstrapError):
            plugin.load_setup_commands(self.source)

    def test_unknown_schema_fields_are_rejected(self):
        command = self.command("bad", "pass")
        command["shell"] = True
        self.write_setup([command])
        with self.assertRaises(plugin.BootstrapError):
            plugin.load_setup_commands(self.source)

    def test_copy_failure_prevents_setup(self):
        (self.source / ".env").write_text("new", encoding="utf-8")
        self.write_copy_list([".env"])
        self.write_setup([self.command("marker", "from pathlib import Path; Path('marker').touch()")])
        with mock.patch.object(plugin, "sync_paths", side_effect=plugin.BootstrapError("copy failed")):
            with self.assertRaises(plugin.BootstrapError):
                plugin.execute_action("bootstrap", self.context, self.state_dir)
        self.assertFalse((self.target / "marker").exists())


class StateAndManagementTests(GitRepositoryTestCase):
    def test_target_lock_rejects_concurrent_operation(self):
        key = plugin.target_key(self.context)
        with plugin.TargetLock(self.state_dir, key):
            with self.assertRaisesRegex(plugin.BootstrapError, "already running"):
                with plugin.TargetLock(self.state_dir, key):
                    pass

    def test_last_successful_result_is_visible_in_status(self):
        (self.source / ".env").write_text("new", encoding="utf-8")
        self.write_copy_list([".env"])
        plugin.execute_action("sync", self.context, self.state_dir)
        lines, invalid = plugin.status_lines(self.context, self.state_dir)
        self.assertFalse(invalid)
        self.assertIn("Last run: status=success, action=sync, copied=1, setup=0", lines)

    def test_invalid_list_is_visible_and_status_is_nonzero(self):
        self.write_copy_list(["../escape"])
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = plugin.main(
                ["status", "--target", os.fspath(self.target), "--state-dir", os.fspath(self.state_dir)],
                env={},
            )
        self.assertEqual(result, 2)
        self.assertIn("Copy list: INVALID", stdout.getvalue())

    def test_manage_write_excludes_only_local_control_file(self):
        plugin.write_copy_list(self.context, [".env"])
        exclude = self.context.common_git_dir / "info" / "exclude"
        text = exclude.read_text(encoding="utf-8")
        self.assertIn("/.herdr/worktree-copy.list\n", text)
        self.assertNotIn("worktree-setup.json", text)
        self.assertEqual((self.source / ".gitignore").read_text(encoding="utf-8").splitlines()[0], ".env")

    def test_existing_setup_is_excluded_when_management_opens(self):
        self.write_setup([])
        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch("builtins.input", return_value="q"):
                self.assertEqual(plugin.manage(self.context, self.state_dir), 0)
        exclude = self.context.common_git_dir / "info" / "exclude"
        self.assertIn("/.herdr/worktree-setup.json", exclude.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
