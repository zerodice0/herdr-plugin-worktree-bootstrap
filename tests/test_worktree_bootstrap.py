from __future__ import annotations

import contextlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import pty
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
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


def cleanup_decision_worker(connection, state_dir, cleanup_id, env, *, holder, checkout):
    """Spawn-safe worker; barriers surround real Git/filesystem mutations."""
    state_dir = Path(state_dir)
    original_git = plugin._run_git
    original_flock = plugin.fcntl.flock
    original_remove = plugin.remove_leftover_worktree_files

    def mutation_barrier(message):
        connection.send(message)
        if not connection.poll(20) or connection.recv() != "continue":
            raise RuntimeError("cleanup mutation barrier was not released")

    def held_git(repo, arguments, **kwargs):
        if arguments[:2] == ["branch", "-d"]:
            mutation_barrier("git-mutation")
        return original_git(repo, arguments, **kwargs)

    def contended_flock(fd, operation):
        if operation == plugin.fcntl.LOCK_EX:
            # Probe the real OS lock, so the parent cannot release the holder
            # before the second process actually encounters contention.
            try:
                original_flock(fd, operation | plugin.fcntl.LOCK_NB)
            except BlockingIOError:
                connection.send("contended")
            else:
                connection.send("uncontended")
                return
        return original_flock(fd, operation)

    def held_remove(*args, **kwargs):
        mutation_barrier("checkout-mutation")
        return original_remove(*args, **kwargs)

    try:
        with contextlib.ExitStack() as stack:
            if holder:
                stack.enter_context(mock.patch.object(plugin, "_run_git", side_effect=held_git))
                arguments = {"decision": "delete", "confirm": "feature"}
            else:
                stack.enter_context(
                    mock.patch.object(plugin.fcntl, "flock", side_effect=contended_flock)
                )
                arguments = {"decision": "keep"}
                if checkout:
                    stack.enter_context(mock.patch.object(
                        plugin, "remove_leftover_worktree_files", side_effect=held_remove
                    ))
                    arguments = {"checkout": "remove", "confirm_checkout": "feature checkout"}
            result = plugin.decide_pending_cleanup(
                state_dir, cleanup_id=cleanup_id, env=env, **arguments
            )
            connection.send(("result", result))
    except plugin.BootstrapError as exc:
        connection.send(("error", exc.code))
    finally:
        connection.close()


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
        invalid = [
            ".",
            "/absolute",
            "../escape",
            "a/../b",
            "a/./b",
            "a//b",
            "a/",
            "line\nbreak",
            "carriage\rreturn",
        ]
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


class ManagementLauncherTests(unittest.TestCase):
    def test_non_git_repository_has_a_stable_user_facing_reason(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                plugin.NotGitRepository,
                "this workspace is not a Git repository",
            ):
                plugin.resolve_repository(Path(temporary))

    def test_non_git_launcher_notifies_without_opening_a_popup(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(
                plugin,
                "notify_repository_unavailable",
                return_value=True,
            ) as notify_mock:
                with mock.patch.object(plugin, "launch_management_popup") as popup_mock:
                    result = plugin.launch_management_ui("manage", Path(temporary), {})
        self.assertEqual(result, 0)
        popup_mock.assert_not_called()
        reason = notify_mock.call_args.args[0]
        self.assertIsInstance(reason, plugin.NotGitRepository)

    def test_notification_uses_the_native_herdr_command(self):
        completed = subprocess.CompletedProcess([], 0)
        with mock.patch.object(plugin.subprocess, "run", return_value=completed) as run_mock:
            shown = plugin.show_herdr_notification(
                "Worktree Bootstrap unavailable",
                "This workspace is not a Git repository.",
                {"HERDR_BIN_PATH": "/usr/local/bin/herdr"},
            )
        self.assertTrue(shown)
        command = run_mock.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/local/bin/herdr", "notification", "show"])
        self.assertIn("Worktree Bootstrap unavailable", command)
        self.assertEqual(command[command.index("--sound") + 1], "none")

    def test_valid_launcher_opens_the_requested_bounded_popup(self):
        context = plugin.RepositoryContext(
            target=Path("/tmp/repository"),
            source=Path("/tmp/repository"),
            common_git_dir=Path("/tmp/repository/.git"),
            target_is_primary=True,
        )
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with mock.patch.object(plugin.subprocess, "run", return_value=completed) as run_mock:
            plugin.launch_management_popup(
                "setup-manage",
                context,
                {"HERDR_BIN_PATH": "/usr/local/bin/herdr"},
            )
        command = run_mock.call_args.args[0]
        self.assertEqual(command[command.index("--entrypoint") + 1], "setup-manage")
        self.assertEqual(command[command.index("--width") + 1], "84")
        self.assertEqual(command[command.index("--height") + 1], "20")
        self.assertEqual(command[command.index("--cwd") + 1], "/tmp/repository")


class ThemeRenderingTests(unittest.TestCase):
    def inspection(self, **overrides):
        values = {
            "repo_root": Path("/tmp/프로젝트"),
            "branch": "feature/theme-dialog",
            "exists": True,
            "protected": False,
            "protection_reason": None,
            "used_by_worktrees": (),
            "default_ref": "main",
            "merged_into_default": True,
            "upstream": "origin/feature/theme-dialog",
            "ahead": 0,
            "behind": 0,
            "last_commit": "abc1234 Polish cleanup dialog",
            "head_oid": "a" * 40,
        }
        values.update(overrides)
        return plugin.BranchInspection(**values)

    def test_loads_herdr_theme_and_custom_overrides_without_full_toml_dependency(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_text(
                """
[theme]
name = "tokyo-night" # active theme
auto_switch = true
dark_name = 'dracula'
light_name = "tokyo-night-day"

[theme.custom]
accent = "#abcdef"
red = "rgb(200, 10, 20)"

[ui]
accent = "cyan"
""",
                encoding="utf-8",
            )
            settings = plugin.load_theme_settings({}, config_path=config)
        self.assertEqual(settings.name, "tokyo-night")
        self.assertTrue(settings.auto_switch)
        self.assertEqual(settings.dark_name, "dracula")
        self.assertEqual(settings.light_name, "tokyo-night-day")
        self.assertEqual(settings.custom["accent"], "#abcdef")
        self.assertEqual(settings.custom["red"], "rgb(200, 10, 20)")
        self.assertEqual(settings.legacy_accent, "cyan")

    def test_missing_or_unreadable_theme_config_uses_catppuccin(self):
        settings = plugin.load_theme_settings({}, config_path=Path("/does/not/exist"))
        palette = plugin.resolve_theme_palette(settings)
        self.assertEqual(palette.name, "catppuccin")
        self.assertEqual(palette.accent, (137, 180, 250))

    def test_auto_switch_uses_same_light_dark_sibling_names_as_herdr(self):
        settings = plugin.ThemeSettings(name="tokyo-night", auto_switch=True)
        self.assertEqual(
            plugin.resolve_theme_palette(settings, appearance="dark").name,
            "tokyo-night",
        )
        light = plugin.resolve_theme_palette(settings, appearance="light")
        self.assertEqual(light.name, "tokyo-night-day")
        self.assertEqual(light.accent, (46, 125, 233))

    def test_custom_colors_override_semantic_palette_and_ignore_invalid_values(self):
        settings = plugin.ThemeSettings(
            name="nord",
            custom={"accent": "#abc", "red": "rgb(1, 2, 3)", "green": "invalid"},
        )
        palette = plugin.resolve_theme_palette(settings)
        self.assertEqual(palette.accent, (170, 187, 204))
        self.assertEqual(palette.danger, (1, 2, 3))
        self.assertEqual(palette.positive, (163, 190, 140))

    def test_terminal_background_response_drives_auto_switch_appearance(self):
        self.assertEqual(
            plugin._appearance_from_terminal_response("\x1b]11;rgb:1111/2222/3333\x1b\\"),
            "dark",
        )
        self.assertEqual(
            plugin._appearance_from_terminal_response("\x1b]11;#f0f0f0\x07"),
            "light",
        )
        self.assertIsNone(plugin._appearance_from_terminal_response("not an OSC response"))

    def test_themed_dialog_uses_tokyo_night_colors_and_respects_display_width(self):
        palette = plugin.resolve_theme_palette(plugin.ThemeSettings(name="tokyo-night"))
        lines = plugin.render_cleanup_dialog(
            self.inspection(),
            {"worktree_path": "/tmp/프로젝트 worktree", "forced_worktree_removal": False},
            palette,
            columns=72,
            decorated=True,
            color_enabled=True,
        )
        rendered = "\n".join(lines)
        self.assertIn("\033[38;2;122;162;247m", rendered)
        self.assertIn("MERGED", rendered)
        self.assertIn("Remote branches are never changed", rendered)
        self.assertNotIn("╭", rendered)
        self.assertNotIn("│", rendered)
        ansi = re.compile(r"\x1b\[[0-9;]*m")
        visible_rendered = ansi.sub("", rendered)
        self.assertIn(
            "ENTER Keep    D Delete safely    F Force…    S Later    Q Close",
            visible_rendered,
        )
        for line in lines:
            visible = ansi.sub("", line)
            self.assertLessEqual(plugin._display_width(visible), 72)

    def test_action_footer_uses_one_line_when_it_fits_and_balanced_rows_when_needed(self):
        palette = plugin.resolve_theme_palette(plugin.ThemeSettings(name="tokyo-night"))
        record = {"worktree_path": "/tmp/worktree", "forced_worktree_removal": False}

        def action_lines(columns):
            lines = plugin.render_cleanup_dialog(
                self.inspection(),
                record,
                palette,
                columns=columns,
                decorated=True,
                color_enabled=False,
            )
            return [
                line.strip()
                for line in lines
                if "ENTER Keep" in line or "F Force…" in line
            ]

        self.assertEqual(
            action_lines(65),
            ["ENTER Keep    D Delete safely    F Force…    S Later    Q Close"],
        )
        expected_compact = [
            "ENTER Keep    D Delete safely",
            "F Force…    S Later    Q Close",
        ]
        self.assertEqual(action_lines(64), expected_compact)
        self.assertEqual(action_lines(32), expected_compact)

    def test_very_narrow_tty_uses_visible_line_input_fallback(self):
        palette = plugin.resolve_theme_palette(plugin.ThemeSettings(name="tokyo-night"))
        lines = plugin.render_cleanup_dialog(
            self.inspection(),
            {"worktree_path": "/tmp/worktree", "forced_worktree_removal": False},
            palette,
            columns=31,
            decorated=True,
            color_enabled=False,
        )
        self.assertFalse(plugin._supports_action_footer(True, 31))
        self.assertTrue(plugin._supports_action_footer(True, 32))
        self.assertNotIn("ENTER Keep", "\n".join(lines))
        self.assertIn("Herdr branch cleanup review", lines)

    def test_non_tty_dialog_remains_plain_and_log_friendly(self):
        palette = plugin.resolve_theme_palette(plugin.ThemeSettings(name="tokyo-night"))
        lines = plugin.render_cleanup_dialog(
            self.inspection(protected=True),
            {"worktree_path": "/tmp/worktree", "forced_worktree_removal": False},
            palette,
            columns=80,
            decorated=False,
            color_enabled=False,
        )
        rendered = "\n".join(lines)
        self.assertIn("Deletion blocked", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("╭", rendered)

    def test_force_delete_dialog_is_flat_inside_herdr_popup(self):
        palette = plugin.resolve_theme_palette(plugin.ThemeSettings(name="dracula"))
        lines = plugin.render_force_delete_dialog(
            self.inspection(merged_into_default=False),
            palette,
            columns=70,
            decorated=True,
            color_enabled=False,
        )
        rendered = "\n".join(lines)
        self.assertIn("Destructive action", rendered)
        self.assertIn("feature/theme-dialog", rendered)
        self.assertNotIn("╭", rendered)
        self.assertNotIn("│", rendered)

    def test_single_key_reader_does_not_echo_and_restores_terminal(self):
        master, slave = pty.openpty()
        output = io.StringIO()
        original = plugin.termios.tcgetattr(slave)
        writer = threading.Timer(0.05, os.write, args=(master, b"D"))
        writer.start()
        try:
            self.assertEqual(
                plugin.normalize_cleanup_choice(
                    plugin.read_single_key(input_fd=slave, output_stream=output)
                ),
                "d",
            )
            restored = plugin.termios.tcgetattr(slave)
        finally:
            writer.join()
            os.close(master)
            os.close(slave)
        restored_mode = restored[3] & (plugin.termios.ICANON | plugin.termios.ECHO)
        original_mode = original[3] & (plugin.termios.ICANON | plugin.termios.ECHO)
        self.assertEqual(restored_mode, original_mode)
        self.assertEqual(restored[6][plugin.termios.VMIN], original[6][plugin.termios.VMIN])
        self.assertEqual(restored[6][plugin.termios.VTIME], original[6][plugin.termios.VTIME])
        self.assertEqual(output.getvalue(), "\033[?25l\033[?25h")

    def test_single_key_reader_preserves_arrow_key_sequences(self):
        for encoded, expected in (
            (b"\x1b[A", "up"),
            (b"\x1b[B", "down"),
            (b"\x1bOA", "up"),
            (b"\x1bOB", "down"),
        ):
            with self.subTest(expected=expected):
                master, slave = pty.openpty()
                output = io.StringIO()
                writer = threading.Timer(0.05, os.write, args=(master, encoded))
                writer.start()
                try:
                    self.assertEqual(
                        plugin.read_single_key(input_fd=slave, output_stream=output),
                        expected,
                    )
                finally:
                    writer.join()
                    os.close(master)
                    os.close(slave)

    def test_single_key_timeout_enter_and_eof_are_distinct_and_restore_terminal(self):
        for event in ("timeout", "enter", "eof", "error"):
            with self.subTest(event=event):
                master, slave = pty.openpty()
                output = io.StringIO()
                original = plugin.termios.tcgetattr(slave)
                try:
                    if event == "enter":
                        os.write(master, b"\n")
                    with contextlib.ExitStack() as stack:
                        if event == "eof":
                            stack.enter_context(mock.patch.object(plugin.os, "read", return_value=b""))
                        elif event == "error":
                            stack.enter_context(mock.patch.object(
                                plugin.os, "read", side_effect=OSError("fixture read failure")
                            ))
                        if event in ("eof", "error"):
                            stack.enter_context(mock.patch.object(
                                plugin.select, "select", return_value=([slave], [], [])
                            ))
                            with self.assertRaises(EOFError if event == "eof" else OSError):
                                plugin.read_single_key(
                                    input_fd=slave, output_stream=output, timeout_seconds=0
                                )
                        else:
                            value = plugin.read_single_key(
                                input_fd=slave, output_stream=output,
                                timeout_seconds=1 if event == "enter" else 0,
                            )
                            self.assertEqual(value, "" if event == "enter" else None)
                    self.assertEqual(plugin.termios.tcgetattr(slave), original)
                    self.assertTrue(output.getvalue().endswith("\033[?25h"))
                finally:
                    os.close(master)
                    os.close(slave)

    def test_cleanup_choice_supports_enter_escape_and_korean_layout_keys(self):
        self.assertEqual(plugin.normalize_cleanup_choice(""), "")
        self.assertEqual(plugin.normalize_cleanup_choice("ㅇ"), "d")
        self.assertEqual(plugin.normalize_cleanup_choice("ㄹ"), "f")
        self.assertEqual(plugin.normalize_cleanup_choice("ㄴ"), "s")
        self.assertEqual(plugin.normalize_cleanup_choice("ㅂ"), "q")
        self.assertEqual(plugin.normalize_cleanup_choice("ㅏ"), "k")
        self.assertEqual(plugin.normalize_cleanup_choice("escape"), "q")

    def test_management_dashboard_hides_low_value_blocked_paths_by_default(self):
        palette = plugin.resolve_theme_palette(plugin.ThemeSettings(name="tokyo-night"))
        context = plugin.RepositoryContext(
            target=Path("/tmp/repo-worktree"),
            source=Path("/tmp/repo"),
            common_git_dir=Path("/tmp/repo/.git"),
            target_is_primary=False,
        )
        statuses = [
            plugin.CopyEntryStatus(".env", True, False, "ignored", "eligible"),
        ]
        lines = plugin.render_management_screen(
            context,
            [".env"],
            statuses,
            [plugin.SetupCommand("Install dependencies", ("npm", "ci"), 900)],
            [
                (".env", "included"),
                (".cache", "can add/ignored"),
                ("README.md", "cannot add/tracked"),
                ("notes.txt", "cannot add/unignored"),
            ],
            palette,
            columns=96,
            rows=28,
            decorated=True,
            color_enabled=False,
        )
        rendered = "\n".join(lines)
        self.assertIn("COPY PLAN  1 selected  ·  1 root available", rendered)
        self.assertIn("SETUP  1 command", rendered)
        self.assertIn("A Add files", rendered)
        self.assertIn("D Stop copying", rendered)
        self.assertIn("I Type path", rendered)
        self.assertNotIn("README.md", rendered)
        self.assertNotIn("notes.txt", rendered)
        self.assertNotIn("╭", rendered)
        self.assertNotIn("│", rendered)

    def test_management_dashboard_is_responsive_and_details_are_opt_in(self):
        palette = plugin.resolve_theme_palette(plugin.ThemeSettings(name="tokyo-night"))
        context = plugin.RepositoryContext(
            target=Path("/tmp/repo"),
            source=Path("/tmp/repo"),
            common_git_dir=Path("/tmp/repo/.git"),
            target_is_primary=True,
        )
        lines = plugin.render_management_screen(
            context,
            [],
            [],
            [],
            [
                (".cache", "can add/ignored"),
                ("README.md", "cannot add/tracked"),
            ],
            palette,
            columns=48,
            rows=20,
            decorated=True,
            color_enabled=False,
            details_visible=True,
        )
        rendered = "\n".join(lines)
        self.assertIn("REPOSITORY PATHS  1 tracked", rendered)
        self.assertIn(".cache", rendered)
        self.assertNotIn("README.md", rendered)
        self.assertIn("V Hide paths", rendered)
        for line in lines:
            self.assertLessEqual(plugin._display_width(line), 48)

    def test_management_choices_support_single_keys_and_korean_layout(self):
        self.assertEqual(plugin.normalize_manage_choice("ㅊ"), "c")
        self.assertEqual(plugin.normalize_manage_choice("ㅍ"), "v")
        self.assertEqual(plugin.normalize_manage_choice("ㅛ"), "y")
        with mock.patch.object(plugin, "read_single_key", return_value="C"):
            with mock.patch("builtins.input") as line_input:
                self.assertEqual(plugin._read_manage_choice(single_key_mode=True), "c")
        line_input.assert_not_called()

    def test_management_arrow_keys_map_to_scrolling(self):
        self.assertEqual(plugin.normalize_manage_choice("up"), "k")
        self.assertEqual(plugin.normalize_manage_choice("down"), "j")
        self.assertEqual(plugin.normalize_manage_choice("escape"), "q")

    def test_management_dashboard_scrolls_through_every_registered_value(self):
        palette = plugin.resolve_theme_palette(plugin.ThemeSettings(name="tokyo-night"))
        context = plugin.RepositoryContext(
            target=Path("/tmp/repo"),
            source=Path("/tmp/repo"),
            common_git_dir=Path("/tmp/repo/.git"),
            target_is_primary=True,
        )
        paths = [f"local/file-{index:02}.txt" for index in range(12)]
        statuses = [
            plugin.CopyEntryStatus(path, True, False, "ignored", "eligible")
            for path in paths
        ]
        commands = [
            plugin.SetupCommand(f"Command {index:02}", ("tool", str(index)), 60)
            for index in range(8)
        ]
        first = plugin._render_management_view(
            context,
            paths,
            statuses,
            commands,
            [],
            palette,
            columns=80,
            rows=20,
            decorated=True,
            color_enabled=False,
        )
        last = plugin._render_management_view(
            context,
            paths,
            statuses,
            commands,
            [],
            palette,
            columns=80,
            rows=20,
            decorated=True,
            color_enabled=False,
            scroll_offset=10**9,
        )
        first_text = "\n".join(first.lines)
        last_text = "\n".join(last.lines)
        self.assertGreater(first.maximum_offset, 0)
        self.assertIn("SCROLL", first_text)
        self.assertIn("K/↑ up  J/↓ down", first_text)
        self.assertIn(paths[0], first_text)
        self.assertNotIn(paths[-1], first_text)
        self.assertIn("Command 07", last_text)
        self.assertEqual(last.offset, last.maximum_offset)
        self.assertNotIn("… 8 more", first_text)
        self.assertLessEqual(len(first.lines), 20)
        self.assertLessEqual(len(last.lines), 20)

    def test_setup_dashboard_scrolls_without_reusing_j_or_k_for_mutations(self):
        palette = plugin.resolve_theme_palette(plugin.ThemeSettings(name="tokyo-night"))
        context = plugin.RepositoryContext(
            target=Path("/tmp/repo"),
            source=Path("/tmp/repo"),
            common_git_dir=Path("/tmp/repo/.git"),
            target_is_primary=True,
        )
        commands = [
            plugin.SetupCommand(f"Command {index:02}", ("tool", str(index)), 60)
            for index in range(10)
        ]
        screen = plugin._render_setup_management_view(
            context,
            commands,
            palette,
            columns=84,
            rows=20,
            decorated=True,
            color_enabled=False,
            scroll_offset=10**9,
        )
        rendered = "\n".join(screen.lines)
        self.assertIn("Command 09", rendered)
        self.assertIn("M Move down", rendered)
        self.assertIn("Z Remove config", rendered)
        self.assertIn("K/↑ up  J/↓ down", rendered)

    def test_scroll_hint_keeps_both_directions_visible_on_narrow_terminals(self):
        palette = plugin.resolve_theme_palette(plugin.ThemeSettings(name="tokyo-night"))
        line = plugin._scroll_indicator(
            2,
            4,
            30,
            28,
            palette,
            color_enabled=False,
        )
        self.assertIn("SCROLL", line)
        self.assertIn("K↑ J↓", line)
        self.assertLessEqual(plugin._display_width(line), 28)

    def test_terminal_renderer_clears_once_then_refreshes_without_blank_frame(self):
        output = io.StringIO()
        renderer = plugin.TerminalScreenRenderer(decorated=True, stream=output)
        renderer.draw(["first", "screen"])
        renderer.draw(["second", "screen"])
        rendered = output.getvalue()
        self.assertEqual(rendered.count("\033[2J"), 1)
        self.assertEqual(rendered.count("\033[H"), 2)
        self.assertIn("\033[Hsecond\nscreen\033[J", rendered)

    def test_fzf_picker_uses_nul_paths_multi_select_and_modal_navigation(self):
        palette = plugin.resolve_theme_palette(plugin.ThemeSettings(name="tokyo-night"))
        completed = subprocess.CompletedProcess(
            ["fzf"],
            0,
            stdout="unicode-한글\0space dir/file\0".encode("utf-8"),
        )
        with mock.patch.object(plugin, "_fzf_executable", return_value="/opt/bin/fzf"):
            with mock.patch.object(plugin.subprocess, "run", return_value=completed) as run_mock:
                selected = plugin.select_paths_with_fzf(
                    ["space dir/file", "unicode-한글"],
                    palette,
                    operation="add",
                    env={"PATH": "/opt/bin", "TERM": "xterm-256color"},
                )
        self.assertEqual(selected, ["space dir/file", "unicode-한글"])
        command = run_mock.call_args.args[0]
        self.assertIn("--read0", command)
        self.assertIn("--print0", command)
        self.assertIn("--multi", command)
        self.assertIn("--disabled", command)
        binding = next(value for value in command if value.startswith("--bind="))
        self.assertIn("j:down,k:up,space:toggle", binding)
        self.assertIn("/:enable-search", binding)
        self.assertIn("esc:abort", binding)
        self.assertNotIn("unbind(esc)", binding)
        self.assertEqual(
            run_mock.call_args.kwargs["input"],
            "space dir/file\0unicode-한글\0".encode("utf-8"),
        )
        self.assertEqual(run_mock.call_args.kwargs["env"]["FZF_DEFAULT_OPTS"], "")

    def test_fzf_escape_cancellation_returns_no_selection(self):
        palette = plugin.resolve_theme_palette(plugin.ThemeSettings(name="tokyo-night"))
        completed = subprocess.CompletedProcess(["fzf"], 130, stdout=b"")
        with mock.patch.object(plugin, "_fzf_executable", return_value="/opt/bin/fzf"):
            with mock.patch.object(plugin.subprocess, "run", return_value=completed):
                self.assertIsNone(
                    plugin.select_paths_with_fzf(
                        ["alpha", "beta"],
                        palette,
                        operation="remove",
                        env={"PATH": "/opt/bin"},
                    )
                )


class GitRepositoryTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config_dir = self.root / "plugin-config"
        self.config_dir.mkdir()
        self.config_env = {"HERDR_PLUGIN_CONFIG_DIR": os.fspath(self.config_dir)}
        config_patch = mock.patch.dict(os.environ, self.config_env)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.source = self.root / "primary checkout"
        self.target = self.root / "feature checkout"
        self.state_dir = self.root / "state"
        self.herdr_config = self.root / "herdr-config.toml"
        self.herdr_config.write_text(
            f'[worktrees]\ndirectory = "{self.root}"\n',
            encoding="utf-8",
        )
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

    def test_bootstrap_entrypoint_caches_worktree_even_without_configuration(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = plugin.main(
                [
                    "bootstrap",
                    "--target",
                    os.fspath(self.target),
                    "--state-dir",
                    os.fspath(self.state_dir),
                ],
                env=self.config_env,
            )
        self.assertEqual(result, 0)
        mapping = plugin.load_worktree_mapping(self.state_dir, self.target)
        self.assertEqual(mapping["branch"], "feature")
        self.assertEqual(Path(mapping["repo_root"]), self.source.resolve())


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

    def test_setup_writer_round_trips_and_excludes_local_configuration(self):
        command = plugin.SetupCommand("Install dependencies", ("npm", "ci"), 900)
        plugin.write_setup_commands(self.context, [command])
        self.assertEqual(plugin.load_setup_commands(self.source), [command])
        exclude = self.context.common_git_dir / "info" / "exclude"
        self.assertIn("/.herdr/worktree-setup.json\n", exclude.read_text(encoding="utf-8"))

    def test_setup_argv_editor_preserves_quoted_arguments_without_shell_expansion(self):
        self.assertEqual(
            plugin._parse_setup_argv('tool "local data/input.json" $HOME'),
            ("tool", "local data/input.json", "$HOME"),
        )

    def test_setup_editor_adds_and_edits_commands(self):
        answers = iter(
            [
                "n",
                "Install dependencies",
                "npm ci",
                "300",
                "",
                "e",
                "1",
                "",
                "npm install",
                "600",
                "",
                "q",
            ]
        )
        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch("builtins.input", side_effect=answers):
                self.assertEqual(plugin.manage_setup_commands(self.context, self.state_dir, []), 0)
        self.assertEqual(
            plugin.load_setup_commands(self.source),
            [plugin.SetupCommand("Install dependencies", ("npm", "install"), 600)],
        )

    def test_setup_editor_reorders_and_deletes_with_confirmation(self):
        commands = [
            plugin.SetupCommand("First", ("first",), 30),
            plugin.SetupCommand("Second", ("second",), 60),
        ]
        plugin.write_setup_commands(self.context, commands)
        answers = iter(["u", "2", "", "x", "2", "y", "", "q"])
        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch("builtins.input", side_effect=answers):
                self.assertEqual(plugin.manage_setup_commands(self.context, self.state_dir, commands), 0)
        self.assertEqual(
            plugin.load_setup_commands(self.source),
            [plugin.SetupCommand("Second", ("second",), 60)],
        )

    def test_setup_editor_runs_setup_now(self):
        command = plugin.SetupCommand(
            "Create marker",
            (sys.executable, "-c", "from pathlib import Path; Path('setup-marker').touch()"),
            5,
        )
        plugin.write_setup_commands(self.context, [command])
        answers = iter(["r", "", "q"])
        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch("builtins.input", side_effect=answers):
                self.assertEqual(
                    plugin.manage_setup_commands(self.context, self.state_dir, [command]),
                    0,
                )
        self.assertTrue((self.target / "setup-marker").exists())

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


class LifecycleAndInventoryTests(GitRepositoryTestCase):
    def lifecycle_command(self, name, code, timeout=5):
        return plugin.SetupCommand(name, (sys.executable, "-c", code), timeout)

    def test_version_one_setup_is_loaded_as_on_created_lifecycle(self):
        self.write_setup(
            [
                {
                    "name": "legacy",
                    "argv": ["tool", "arg"],
                    "timeout_seconds": 30,
                }
            ]
        )
        lifecycle = plugin.load_lifecycle_commands(self.source)
        self.assertEqual([command.name for command in lifecycle["on_created"]], ["legacy"])
        self.assertEqual(lifecycle["before_remove"], [])
        self.assertEqual(lifecycle["after_remove"], [])

    def test_lifecycle_writer_round_trips_all_phases(self):
        hooks = {
            "on_created": [plugin.SetupCommand("create", ("create-tool",), 10)],
            "before_remove": [plugin.SetupCommand("before", ("before-tool",), 20)],
            "after_remove": [plugin.SetupCommand("after", ("after-tool",), 30)],
        }
        plugin.write_lifecycle_commands(self.context, hooks)
        loaded = plugin.load_lifecycle_commands(self.source)
        self.assertEqual(loaded, hooks)
        document = json.loads((self.source / plugin.SETUP_FILE).read_text(encoding="utf-8"))
        self.assertEqual(document["version"], 2)

    def test_version_two_rejects_unknown_lifecycle_phase(self):
        control = self.source / plugin.SETUP_FILE
        control.parent.mkdir(parents=True, exist_ok=True)
        control.write_text(
            json.dumps({
                "version": 2,
                "hooks": {
                    "on_created": [],
                    "before_remove": [],
                    "after_remove": [],
                    "during_remove": [],
                },
            }),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(plugin.BootstrapError, "unknown lifecycle hook"):
            plugin.load_lifecycle_commands(self.source)

    def test_inventory_lifecycle_editor_configures_before_remove(self):
        answers = iter(
            [
                "2",
                "n",
                "Stop services",
                "stop-services --worktree",
                "45",
                "q",
                "q",
            ]
        )
        output = []
        self.assertEqual(
            plugin.manage_lifecycle_configuration(
                self.context,
                self.state_dir,
                input_fn=lambda prompt: next(answers),
                output_fn=output.append,
            ),
            0,
        )
        lifecycle = plugin.load_lifecycle_commands(self.source)
        self.assertEqual(
            lifecycle["before_remove"],
            [plugin.SetupCommand("Stop services", ("stop-services", "--worktree"), 45)],
        )

    def test_inventory_reconciles_git_branch_and_residue_state(self):
        plugin.record_worktree_mapping(self.context, self.state_dir)
        active = plugin.collect_worktree_inventory(self.source, self.state_dir)
        feature = next(item for item in active if item.branch == "feature")
        self.assertEqual(feature.status, "active")
        self.assertTrue(feature.registered)
        run(["git", "worktree", "remove", os.fspath(self.target)], cwd=self.source)
        branch_only = plugin.collect_worktree_inventory(self.source, self.state_dir)
        feature = next(item for item in branch_only if item.branch == "feature")
        self.assertEqual(feature.status, "branch-only")
        (self.target / "ios").mkdir(parents=True)
        (self.target / "ios" / "cache").write_text("generated", encoding="utf-8")
        residue = plugin.collect_worktree_inventory(self.source, self.state_dir)
        feature = next(item for item in residue if item.branch == "feature")
        self.assertEqual(feature.status, "residue")
        self.assertEqual(feature.leftover.entry_count, 1)

    def test_inventory_discovers_unrecorded_residue_under_configured_root(self):
        worktree_root = self.root / "managed-worktrees"
        orphan = worktree_root / self.source.name / "old-feature" / "ios"
        orphan.mkdir(parents=True)
        (orphan / "cache").write_text("generated", encoding="utf-8")
        items = plugin.collect_worktree_inventory(
            self.source,
            self.state_dir,
            worktree_root=worktree_root,
        )
        discovered = next(item for item in items if item.worktree_path.name == "old-feature")
        self.assertEqual(discovered.status, "residue")
        self.assertFalse(discovered.provenance_verified)
        self.assertIsNone(discovered.branch)

    def test_inventory_reads_configured_worktree_root(self):
        config = self.root / "config.toml"
        configured = self.root / "managed worktrees"
        config.write_text(
            f'[worktrees]\ndirectory = "{configured}"\n',
            encoding="utf-8",
        )
        self.assertEqual(
            plugin.load_worktree_directory({}, config_path=config),
            configured.resolve(),
        )

    def test_before_remove_lifecycle_runs_with_structured_environment(self):
        marker_code = (
            "import os; from pathlib import Path; "
            "(Path(os.environ['HERDR_PRIMARY_PATH']) / 'before-marker').write_text("
            "os.environ['HERDR_WORKTREE_BRANCH'])"
        )
        plugin.write_lifecycle_commands(
            self.context,
            {
                "on_created": [],
                "before_remove": [self.lifecycle_command("before", marker_code)],
                "after_remove": [],
            },
        )
        plugin.record_worktree_mapping(self.context, self.state_dir)
        item = next(
            item
            for item in plugin.collect_worktree_inventory(self.source, self.state_dir)
            if item.branch == "feature"
        )
        with mock.patch.object(plugin, "query_herdr_workspace_id", return_value="workspace-2"):
            with mock.patch.object(plugin, "_herdr_command", return_value="/usr/bin/true"):
                result = plugin.remove_managed_worktree(item, self.state_dir, {})
        self.assertEqual((self.source / "before-marker").read_text(), "feature")
        self.assertEqual(result["before_remove_status"], "success")
        self.assertEqual(result["removal_operation_status"], "requested")

    def test_on_created_lifecycle_receives_structured_environment(self):
        marker_code = (
            "import os; from pathlib import Path; "
            "Path('created-env').write_text('|'.join(["
            "os.environ['HERDR_WORKTREE_PATH'], "
            "os.environ['HERDR_PRIMARY_PATH'], "
            "os.environ['HERDR_WORKTREE_BRANCH']]))"
        )
        plugin.write_lifecycle_commands(
            self.context,
            {
                "on_created": [self.lifecycle_command("created", marker_code)],
                "before_remove": [],
                "after_remove": [],
            },
        )
        plugin.execute_action("setup", self.context, self.state_dir)
        target, primary, branch = (self.target / "created-env").read_text().split("|")
        self.assertEqual(Path(target), self.target.resolve())
        self.assertEqual(Path(primary), self.source.resolve())
        self.assertEqual(branch, "feature")

    def test_before_remove_generated_changes_block_unforced_removal(self):
        plugin.write_lifecycle_commands(
            self.context,
            {
                "on_created": [],
                "before_remove": [
                    self.lifecycle_command(
                        "write output",
                        "from pathlib import Path; Path('generated').touch()",
                    )
                ],
                "after_remove": [],
            },
        )
        plugin.record_worktree_mapping(self.context, self.state_dir)
        item = next(
            item
            for item in plugin.collect_worktree_inventory(self.source, self.state_dir)
            if item.branch == "feature"
        )
        with mock.patch.object(plugin, "query_herdr_workspace_id", return_value="workspace-2"):
            with mock.patch.object(plugin, "_herdr_command") as herdr_command:
                with self.assertRaisesRegex(plugin.BootstrapError, "after before-remove"):
                    plugin.remove_managed_worktree(item, self.state_dir, {})
        herdr_command.assert_not_called()
        mapping = plugin.load_worktree_mapping(self.state_dir, self.target)
        self.assertEqual(mapping["removal_operation_status"], "blocked")

    def test_after_remove_lifecycle_runs_from_primary_checkout_once(self):
        marker_code = (
            "import os; from pathlib import Path; "
            "Path('after-marker').write_text(os.environ['HERDR_WORKTREE_PATH'])"
        )
        plugin.write_lifecycle_commands(
            self.context,
            {
                "on_created": [],
                "before_remove": [],
                "after_remove": [self.lifecycle_command("after", marker_code)],
            },
        )
        plugin.record_worktree_mapping(self.context, self.state_dir)
        run(["git", "worktree", "remove", os.fspath(self.target)], cwd=self.source)
        event = {
            **self.config_env,
            "HERDR_PLUGIN_EVENT": "worktree.removed",
            "HERDR_PLUGIN_EVENT_JSON": json.dumps(
                {
                    "event": "worktree.removed",
                    "data": {
                        "workspace": {
                            "worktree": {"repo_root": os.fspath(self.source.resolve())}
                        },
                        "worktree": {
                            "path": os.fspath(self.target.resolve()),
                            "branch": "feature",
                            "is_detached": False,
                        },
                        "forced": False,
                    },
                }
            ),
        }
        plugin.handle_branch_cleanup_event(event, self.state_dir, open_popup=False)
        plugin.run_after_remove_lifecycle(event, self.state_dir)
        self.assertEqual((self.source / "after-marker").read_text(), os.fspath(self.target.resolve()))
        mapping = plugin.load_worktree_mapping(self.state_dir, self.target)
        self.assertEqual(mapping["after_remove_status"], "success")
        self.assertEqual(mapping["after_remove_completed_count"], 1)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_leftover_cleanup_rejects_replaced_symlink(self):
        plugin.record_worktree_mapping(self.context, self.state_dir)
        run(["git", "worktree", "remove", os.fspath(self.target)], cwd=self.source)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("safe", encoding="utf-8")
        os.symlink(os.fspath(outside), self.target)
        inspection = plugin.inspect_leftover_path(self.target)
        self.assertTrue(inspection.is_symlink)
        with self.assertRaisesRegex(plugin.BootstrapError, "real directory"):
            plugin.remove_leftover_worktree_files(
                self.source,
                self.target,
                worktree_root=self.root,
                provenance_verified=True,
                expected_device=inspection.device,
                expected_inode=inspection.inode,
            )
        self.assertTrue((outside / "keep").exists())

    def test_leftover_cleanup_rejects_registered_worktree(self):
        inspection = plugin.inspect_leftover_path(self.target)
        with self.assertRaisesRegex(plugin.BootstrapError, "still registered"):
            plugin.remove_leftover_worktree_files(
                self.source,
                self.target,
                worktree_root=self.root,
                provenance_verified=True,
                expected_device=inspection.device,
                expected_inode=inspection.inode,
            )

    def test_leftover_cleanup_rejects_path_outside_configured_root(self):
        run(["git", "worktree", "remove", os.fspath(self.target)], cwd=self.source)
        (self.target / "ios").mkdir(parents=True)
        inspection = plugin.inspect_leftover_path(self.target)
        with self.assertRaisesRegex(plugin.BootstrapError, "outside the configured"):
            plugin.remove_leftover_worktree_files(
                self.source,
                self.target,
                worktree_root=self.root / "different-root",
                provenance_verified=True,
                expected_device=inspection.device,
                expected_inode=inspection.inode,
            )
        self.assertTrue(self.target.exists())

    def test_inventory_parser_has_direct_entrypoints(self):
        parser = plugin.build_parser()
        self.assertEqual(parser.parse_args(["inventory-launch"]).action, "inventory-launch")
        self.assertEqual(parser.parse_args(["inventory"]).action, "inventory")


class StateAndManagementTests(GitRepositoryTestCase):
    def test_ignored_picker_candidates_include_nested_paths_without_expanding_directories(self):
        (self.source / ".env").write_text("secret", encoding="utf-8")
        (self.source / "cache" / "deep").mkdir(parents=True)
        (self.source / "cache" / "deep" / "artifact").write_text("cache", encoding="utf-8")
        (self.source / "space dir").mkdir()
        (self.source / "space dir" / "local file").write_text("space", encoding="utf-8")
        (self.source / "unicode-한글").mkdir()
        (self.source / "unicode-한글" / "설정").write_text("value", encoding="utf-8")
        plugin.write_copy_list(self.context, [".env"])

        candidates = plugin.list_ignored_path_candidates(self.context, [".env"])

        self.assertIn("cache", candidates)
        self.assertIn("space dir", candidates)
        self.assertIn("unicode-한글", candidates)
        self.assertNotIn(".env", candidates)
        self.assertFalse(any(path.startswith("cache/") for path in candidates))
        self.assertFalse(any(path == ".herdr" or path.startswith(".herdr/") for path in candidates))

    def test_setup_management_has_a_direct_cli_entrypoint(self):
        self.assertEqual(
            plugin.build_parser().parse_args(["setup-manage"]).action,
            "setup-manage",
        )

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
                env=self.config_env,
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


class BranchCleanupTests(GitRepositoryTestCase):
    def removed_event(self, *, workspace=True, branch="feature", detached=False, forced=False):
        workspace_value = None
        if workspace:
            workspace_value = {
                "workspace_id": "w2",
                "worktree": {
                    "repo_root": os.fspath(self.source.resolve()),
                    "repo_key": os.fspath((self.source / ".git").resolve()),
                    "repo_name": self.source.name,
                    "checkout_path": os.fspath(self.target.resolve()),
                    "is_linked_worktree": True,
                },
            }
        return {
            **self.config_env,
            "HERDR_PLUGIN_EVENT": "worktree.removed",
            "HERDR_PLUGIN_EVENT_JSON": json.dumps(
                {
                    "event": "worktree.removed",
                    "data": {
                        "type": "worktree_removed",
                        "workspace_id": "w2",
                        "workspace": workspace_value,
                        "worktree": {
                            "path": os.fspath(self.target.resolve()),
                            "branch": branch,
                            "is_bare": False,
                            "is_detached": detached,
                            "is_prunable": False,
                            "is_linked_worktree": True,
                            "label": branch,
                        },
                        "forced": forced,
                    },
                }
            ),
        }

    def record_and_remove_target(self):
        plugin.record_worktree_mapping(self.context, self.state_dir)
        run(["git", "worktree", "remove", os.fspath(self.target)], cwd=self.source)

    def branch_exists(self, branch):
        result = run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self.source,
            check=False,
        )
        return result.returncode == 0

    def test_removed_event_queues_and_safely_deletes_merged_branch(self):
        self.record_and_remove_target()
        pending = plugin.handle_branch_cleanup_event(
            self.removed_event(), self.state_dir, open_popup=False
        )
        self.assertEqual(pending["branch"], "feature")
        self.assertFalse(pending["popup_opened"])
        inspection = plugin.inspect_branch(self.source, "feature")
        self.assertTrue(inspection.exists)
        self.assertTrue(inspection.merged_into_default)
        output = []
        result = plugin.review_pending_cleanups(
            self.state_dir,
            input_fn=lambda prompt: "d",
            output_fn=output.append,
        )
        self.assertEqual(result, 0)
        self.assertFalse(self.branch_exists("feature"))
        self.assertEqual(plugin.list_pending_cleanups(self.state_dir), [])

    def test_keep_is_default_and_resolves_pending_without_deletion(self):
        self.record_and_remove_target()
        plugin.handle_branch_cleanup_event(self.removed_event(), self.state_dir, open_popup=False)
        plugin.review_pending_cleanups(
            self.state_dir,
            input_fn=lambda prompt: "",
            output_fn=lambda line: None,
        )
        self.assertTrue(self.branch_exists("feature"))
        self.assertEqual(plugin.list_pending_cleanups(self.state_dir), [])
        last = plugin._read_json_file(
            self.state_dir / plugin.BRANCH_CLEANUP_DIR / "last-result.json"
        )
        self.assertEqual(last["outcome"], "kept")

    def test_unmerged_branch_safe_delete_is_refused_and_force_requires_exact_name(self):
        (self.target / "feature-only").write_text("unmerged", encoding="utf-8")
        run(["git", "add", "feature-only"], cwd=self.target)
        run(["git", "commit", "-qm", "feature only"], cwd=self.target)
        self.record_and_remove_target()
        plugin.handle_branch_cleanup_event(self.removed_event(), self.state_dir, open_popup=False)
        answers = iter(["d", "f", "wrong", "f", "feature"])
        output = []
        plugin.review_pending_cleanups(
            self.state_dir,
            input_fn=lambda prompt: next(answers),
            output_fn=output.append,
        )
        self.assertFalse(self.branch_exists("feature"))
        self.assertEqual(plugin.list_pending_cleanups(self.state_dir), [])
        last = plugin._read_json_file(
            self.state_dir / plugin.BRANCH_CLEANUP_DIR / "last-result.json"
        )
        self.assertEqual(last["outcome"], "deleted_forcibly")

    def test_protected_branch_cannot_be_deleted(self):
        primary_branch = run(["git", "branch", "--show-current"], cwd=self.source).stdout.strip()
        pending_id = plugin.pending_cleanup_id(self.source, self.target, primary_branch)
        plugin._atomic_write_json(
            plugin.pending_cleanup_path(self.state_dir, pending_id),
            {
                "version": 1,
                "id": pending_id,
                "created_at": "2026-07-31T00:00:00+00:00",
                "repo_root": os.fspath(self.source.resolve()),
                "worktree_path": os.fspath(self.target.resolve()),
                "branch": primary_branch,
                "forced_worktree_removal": False,
                "status": "pending",
            },
        )
        output = []
        plugin.review_pending_cleanups(
            self.state_dir,
            input_fn=lambda prompt: "k",
            output_fn=output.append,
        )
        self.assertTrue(self.branch_exists(primary_branch))
        with self.assertRaisesRegex(plugin.BootstrapError, "protected"):
            plugin.delete_local_branch(self.source, primary_branch, force=True)

    def test_branch_used_by_another_worktree_is_blocked(self):
        self.record_and_remove_target()
        other = self.root / "other feature checkout"
        run(["git", "worktree", "add", os.fspath(other), "feature"], cwd=self.source)
        plugin.handle_branch_cleanup_event(self.removed_event(), self.state_dir, open_popup=False)
        inspection = plugin.inspect_branch(self.source, "feature")
        self.assertIn(os.fspath(other.resolve()), inspection.used_by_worktrees)
        with self.assertRaisesRegex(plugin.BootstrapError, "another worktree"):
            plugin.delete_local_branch(self.source, "feature")

    def test_branch_change_after_review_is_blocked(self):
        self.record_and_remove_target()
        inspection = plugin.inspect_branch(self.source, "feature")
        with self.assertRaisesRegex(plugin.BootstrapError, "changed after it was displayed"):
            plugin.delete_local_branch(
                self.source,
                "feature",
                expected_oid="0" * 40,
            )
        self.assertTrue(self.branch_exists("feature"))

    def test_workspace_null_uses_cached_primary_checkout(self):
        self.record_and_remove_target()
        pending = plugin.queue_branch_cleanup(
            self.removed_event(workspace=False), self.state_dir
        )
        self.assertEqual(Path(pending["repo_root"]), self.source.resolve())

    def test_detached_removed_worktree_is_a_noop(self):
        plugin.record_worktree_mapping(self.context, self.state_dir)
        mapping_path = plugin.worktree_map_path(self.state_dir, self.target)
        self.assertTrue(mapping_path.exists())
        with self.assertRaises(plugin.NothingToDo):
            plugin.queue_branch_cleanup(
                self.removed_event(branch=None, detached=True), self.state_dir
            )
        self.assertEqual(plugin.list_pending_cleanups(self.state_dir), [])
        self.assertTrue(mapping_path.exists())
        mapping = plugin.load_worktree_mapping(self.state_dir, self.target)
        self.assertEqual(mapping["status"], "removed")
        self.assertIsNone(mapping["branch"])

    def test_leftover_files_are_reviewed_separately_from_the_branch(self):
        self.record_and_remove_target()
        leftover = self.target / "ios" / "DerivedData"
        leftover.mkdir(parents=True)
        (leftover / "cache").write_text("generated", encoding="utf-8")
        pending = plugin.handle_branch_cleanup_event(
            self.removed_event(), self.state_dir, open_popup=False
        )
        self.assertTrue(pending["provenance_verified"])
        answers = iter(["k", "r", self.target.name])
        plugin.review_pending_cleanups(
            self.state_dir,
            input_fn=lambda prompt: next(answers),
            output_fn=lambda line: None,
            env={"HERDR_CONFIG_PATH": os.fspath(self.herdr_config)},
        )
        self.assertFalse(self.target.exists())
        self.assertTrue(self.branch_exists("feature"))
        self.assertEqual(plugin.list_pending_cleanups(self.state_dir), [])

    def test_missing_branch_does_not_hide_leftover_files(self):
        self.record_and_remove_target()
        run(["git", "branch", "-D", "feature"], cwd=self.source)
        leftover = self.target / "ios"
        leftover.mkdir(parents=True)
        (leftover / "cache").write_text("generated", encoding="utf-8")
        plugin.handle_branch_cleanup_event(self.removed_event(), self.state_dir, open_popup=False)
        answers = iter(["r", self.target.name])
        plugin.review_pending_cleanups(
            self.state_dir,
            input_fn=lambda prompt: next(answers),
            output_fn=lambda line: None,
            env={"HERDR_CONFIG_PATH": os.fspath(self.herdr_config)},
        )
        self.assertFalse(self.target.exists())
        self.assertEqual(plugin.list_pending_cleanups(self.state_dir), [])

    def test_popup_launch_passes_only_cleanup_id(self):
        completed = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with mock.patch.object(plugin.subprocess, "run", return_value=completed) as run_mock:
            opened = plugin.launch_branch_cleanup_popup(
                "a" * 24,
                "feature",
                {"HERDR_BIN_PATH": "/usr/local/bin/herdr"},
            )
        self.assertTrue(opened)
        command = run_mock.call_args.args[0]
        self.assertIn("HERDR_BRANCH_CLEANUP_ID=" + "a" * 24, command)
        self.assertNotIn("feature", command)
        self.assertEqual(command[command.index("--width") + 1], "68%")
        self.assertEqual(command[command.index("--height") + 1], "50%")

    def test_event_keeps_pending_when_popup_cannot_open(self):
        self.record_and_remove_target()
        with mock.patch.object(plugin, "launch_branch_cleanup_popup", return_value=False):
            result = plugin.handle_branch_cleanup_event(
                self.removed_event(), self.state_dir, open_popup=True
            )
        self.assertFalse(result["popup_opened"])
        self.assertEqual(len(plugin.list_pending_cleanups(self.state_dir)), 1)


class HeadlessCliTests(GitRepositoryTestCase):
    removed_event = BranchCleanupTests.removed_event
    record_and_remove_target = BranchCleanupTests.record_and_remove_target
    branch_exists = BranchCleanupTests.branch_exists

    def setUp(self):
        super().setUp()
        run(["git", "branch", "-M", "main"], cwd=self.source)
        self.herdr_calls = self.root / "herdr-calls.jsonl"
        self.before_marker = self.source / "before-marker"
        self.fake_herdr = self.root / "fake-herdr"
        workspace_list = {
            "workspaces": [{"workspace_id": "w2", "cwd": os.fspath(self.target.resolve())}]
        }
        self.fake_herdr.write_text(
            f"#!{sys.executable}\n"
            "import json\n"
            "import sys\n"
            "from pathlib import Path\n"
            f"log = Path({os.fspath(self.herdr_calls)!r})\n"
            f"marker = Path({os.fspath(self.before_marker)!r})\n"
            "with log.open('a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps({'argv': sys.argv[1:], "
            "'before_marker_exists': marker.exists()}) + '\\n')\n"
            "if sys.argv[1:] == ['workspace', 'list']:\n"
            f"    print(json.dumps({workspace_list!r}))\n"
            "else:\n"
            "    print('{}')\n",
            encoding="utf-8",
        )
        self.fake_herdr.chmod(0o755)
        self.env = {
            "HOME": os.fspath(self.root / "home"),
            "XDG_CONFIG_HOME": os.fspath(self.root / "xdg-config"),
            "XDG_STATE_HOME": os.fspath(self.root / "xdg-state"),
            "HERDR_CONFIG_PATH": os.fspath(self.herdr_config),
            "HERDR_PLUGIN_CONFIG_DIR": os.fspath(self.config_dir),
            "HERDR_PLUGIN_STATE_DIR": os.fspath(self.state_dir),
            "HERDR_BIN_PATH": os.fspath(self.fake_herdr),
        }

    def invoke_json(self, arguments, *, env=None):
        effective_env = dict(self.env)
        if env:
            effective_env.update(env)
        stdout = io.StringIO()
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with contextlib.ExitStack() as stack:
                    stack.enter_context(
                        mock.patch("builtins.input", side_effect=AssertionError("unexpected input"))
                    )
                    stack.enter_context(mock.patch.object(
                        plugin, "review_pending_cleanups",
                        side_effect=AssertionError("unexpected interactive cleanup review"),
                    ))
                    stack.enter_context(mock.patch.object(
                        plugin, "read_single_key",
                        side_effect=AssertionError("unexpected terminal input"),
                    ))
                    result = plugin.main([*arguments, "--json"], env=effective_env)
            stderr.seek(0)
            errors = stderr.read()
        document = json.loads(stdout.getvalue())
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["action"], arguments[0])
        self.assertIsInstance(document["ok"], bool)
        self.assertTrue(stdout.getvalue().endswith("\n"))
        return result, document, errors

    def assert_cli_error(self, arguments, code):
        result, document, _ = self.invoke_json(arguments)
        self.assertNotEqual(result, 0)
        self.assertFalse(document["ok"])
        self.assertEqual(document["error"]["code"], code)
        return document

    def calls(self):
        if not self.herdr_calls.exists():
            return []
        return [
            json.loads(line)
            for line in self.herdr_calls.read_text(encoding="utf-8").splitlines()
        ]

    def queue_removed_target(self, *, leftover=False, branch="feature"):
        self.record_and_remove_target()
        if leftover:
            (self.target / "ios").mkdir(parents=True)
            (self.target / "ios" / "cache").write_text("generated", encoding="utf-8")
        env = dict(self.env, **self.removed_event(branch=branch))
        with contextlib.redirect_stdout(io.StringIO()):
            return plugin.handle_branch_cleanup_event(env, self.state_dir, open_popup=False)

    def cleanup_arguments(self, pending, *arguments):
        return ["branch-cleanup", "--id", pending["id"], *arguments]

    def last_cleanup_result(self):
        return json.loads(
            (self.state_dir / plugin.BRANCH_CLEANUP_DIR / "last-result.json").read_text(
                encoding="utf-8"
            )
        )

    def test_status_json_reports_copy_eligibility_and_all_lifecycle_phases(self):
        (self.source / ".env").write_text("secret", encoding="utf-8")
        self.write_copy_list([".env"])
        hooks = {
            "on_created": [plugin.SetupCommand("create", ("create-tool", "arg"), 10)],
            "before_remove": [plugin.SetupCommand("before", ("before-tool",), 20)],
            "after_remove": [plugin.SetupCommand("after", ("after-tool",), 30)],
        }
        plugin.write_lifecycle_commands(self.context, hooks)
        (self.config_dir / "settings.json").write_text(
            json.dumps({"version": 1, "branch_cleanup_popup": "never"}),
            encoding="utf-8",
        )
        result, document, _ = self.invoke_json(
            ["status", "--target", os.fspath(self.target)]
        )
        self.assertEqual(result, 0)
        self.assertTrue(document["ok"])
        self.assertEqual(document["source"], os.fspath(self.source.resolve()))
        self.assertEqual(document["target"], os.fspath(self.target.resolve()))
        self.assertEqual(document["common_git_dir"], os.fspath(self.context.common_git_dir))
        self.assertTrue(document["copy_list"]["valid"])
        self.assertIsNone(document["copy_list"]["error"])
        entry, = document["copy_list"]["entries"]
        self.assertEqual(entry["path"], ".env")
        self.assertTrue(entry["source_exists"])
        self.assertFalse(entry["target_exists"])
        self.assertEqual(entry["eligibility"], "ignored")
        self.assertIsInstance(entry["detail"], str)
        self.assertTrue(entry["copyable"])
        self.assertTrue(document["lifecycle"]["valid"])
        self.assertIsNone(document["lifecycle"]["error"])
        for phase, commands in hooks.items():
            command, = commands
            self.assertEqual(
                document["lifecycle"][phase],
                [{
                    "name": command.name,
                    "argv": list(command.argv),
                    "timeout_seconds": command.timeout_seconds,
                }],
            )
        self.assertIsNone(document["last_run"])
        self.assertEqual(document["pending_branch_cleanups"], 0)
        self.assertEqual(document["branch_cleanup_popup"], "never")
        self.assertEqual(self.calls(), [])

    def test_status_json_invalid_setup_is_a_structured_exit_two(self):
        control = self.source / plugin.SETUP_FILE
        control.parent.mkdir(parents=True)
        control.write_text("{", encoding="utf-8")
        result, document, _ = self.invoke_json(
            ["status", "--target", os.fspath(self.target)]
        )
        self.assertEqual(result, 2)
        self.assertFalse(document["ok"])
        self.assertFalse(document["lifecycle"]["valid"])
        self.assertIsInstance(document["lifecycle"]["error"], str)
        for phase in ("on_created", "before_remove", "after_remove"):
            self.assertEqual(document["lifecycle"][phase], [])

    def test_inventory_json_is_noninteractive_and_filters_pending_by_repository(self):
        pending = self.queue_removed_target()
        active_path = self.root / "active checkout"
        run(["git", "worktree", "add", "-qb", "active", os.fspath(active_path)], cwd=self.source)
        other_repo = self.root / "other repository"
        run(["git", "clone", "-q", os.fspath(self.source), os.fspath(other_repo)])
        foreign_event = self.removed_event(branch="main")
        envelope = json.loads(foreign_event["HERDR_PLUGIN_EVENT_JSON"])
        envelope["data"]["workspace"]["worktree"]["repo_root"] = os.fspath(other_repo)
        envelope["data"]["worktree"]["path"] = os.fspath(self.root / "foreign checkout")
        foreign_event["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(envelope)
        foreign = plugin.queue_branch_cleanup(foreign_event, self.state_dir)
        result, document, _ = self.invoke_json(
            ["inventory", "--target", os.fspath(self.source)]
        )
        self.assertEqual(result, 0)
        self.assertTrue(document["ok"])
        self.assertEqual(document["repo_root"], os.fspath(self.source.resolve()))
        self.assertEqual(document["worktree_root"], os.fspath(self.root.resolve()))
        items = {item["worktree_path"]: item for item in document["items"]}
        primary = items[os.fspath(self.source.resolve())]
        self.assertFalse(primary["removable"])
        self.assertEqual(primary["confirm_token"], "main")
        active = items[os.fspath(active_path.resolve())]
        self.assertTrue(active["removable"])
        self.assertEqual(active["confirm_token"], "active")
        self.assertTrue(active["registered"])
        self.assertTrue(active["leftover"]["exists"])
        removed = items[os.fspath(self.target.resolve())]
        self.assertFalse(removed["removable"])
        self.assertEqual(removed["confirm_token"], "feature")
        self.assertFalse(removed["leftover"]["exists"])
        self.assertEqual(
            [item["id"] for item in document["pending_branch_cleanups"]],
            [pending["id"]],
        )
        self.assertNotEqual(pending["id"], foreign["id"])
        self.assertEqual(len(plugin.list_pending_cleanups(self.state_dir)), 2)

    def test_bootstrap_rejects_json_before_making_changes(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                plugin.main(
                    ["bootstrap", "--target", os.fspath(self.target), "--json"],
                    env=self.env,
                )
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(self.state_dir.exists())

    def test_popup_never_still_queues_and_runs_after_remove_without_herdr(self):
        plugin.write_lifecycle_commands(
            self.context,
            {
                "on_created": [],
                "before_remove": [],
                "after_remove": [plugin.SetupCommand(
                    "after",
                    (sys.executable, "-c", "from pathlib import Path; Path('after-marker').touch()"),
                    5,
                )],
            },
        )
        self.record_and_remove_target()
        (self.config_dir / "settings.json").write_text(
            json.dumps({"version": 1, "branch_cleanup_popup": "never"}),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            pending = plugin.handle_branch_cleanup_event(
                dict(self.env, **self.removed_event()), self.state_dir
            )
        self.assertFalse(pending["popup_opened"])
        self.assertTrue(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())
        self.assertTrue((self.source / "after-marker").exists())
        self.assertEqual(self.calls(), [])
        self.assertIn("popup=disabled", stdout.getvalue())

    def test_invalid_popup_settings_fall_back_to_opening_popup(self):
        self.record_and_remove_target()
        for settings in (
            "{",
            json.dumps({"version": 1, "branch_cleanup_popup": []}),
            json.dumps({"version": 1, "branch_cleanup_popup": "sometimes"}),
        ):
            with self.subTest(settings=settings):
                (self.config_dir / "settings.json").write_text(settings, encoding="utf-8")
                previous_calls = len(self.calls())
                with contextlib.redirect_stdout(io.StringIO()):
                    with contextlib.redirect_stderr(io.StringIO()):
                        pending = plugin.handle_branch_cleanup_event(
                            dict(self.env, **self.removed_event()), self.state_dir
                        )
                self.assertTrue(pending["popup_opened"])
                new_calls = self.calls()[previous_calls:]
                popup_calls = [
                    call for call in new_calls
                    if call["argv"][:3] == ["plugin", "pane", "open"]
                ]
                self.assertEqual(len(popup_calls), 1)
                popup_arguments = popup_calls[0]["argv"]
                self.assertEqual(popup_arguments[popup_arguments.index("--placement") + 1], "popup")
                self.assertTrue(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())

    def test_remove_worktree_confirmation_mismatch_never_calls_herdr(self):
        plugin.record_worktree_mapping(self.context, self.state_dir)
        self.assert_cli_error(
            ["remove-worktree", "--target", os.fspath(self.target), "--confirm", "wrong"],
            "confirmation_mismatch",
        )
        self.assertEqual(self.calls(), [])
        self.assertTrue(self.target.exists())
        self.assertTrue((self.target / ".git").exists())

    def test_remove_worktree_dirty_checkout_requires_force(self):
        plugin.record_worktree_mapping(self.context, self.state_dir)
        (self.target / "untracked").write_text("keep", encoding="utf-8")
        self.assert_cli_error(
            ["remove-worktree", "--target", os.fspath(self.target), "--confirm", "feature"],
            "dirty_requires_force",
        )
        self.assertEqual(self.calls(), [])
        self.assertTrue((self.target / "untracked").exists())

    def test_remove_worktree_rejects_primary_checkout(self):
        self.assert_cli_error(
            ["remove-worktree", "--target", os.fspath(self.source), "--confirm", "main"],
            "primary_checkout",
        )
        self.assertEqual(self.calls(), [])
        self.assertTrue((self.source / "tracked.txt").exists())

    def test_remove_worktree_runs_hook_before_herdr_and_returns_event_cleanup_id(self):
        plugin.write_lifecycle_commands(
            self.context,
            {
                "on_created": [],
                "before_remove": [plugin.SetupCommand(
                    "before",
                    (
                        sys.executable,
                        "-c",
                        "from pathlib import Path; "
                        f"Path({os.fspath(self.before_marker)!r}).touch(); "
                        "print('lifecycle-output', flush=True)",
                    ),
                    5,
                )],
                "after_remove": [],
            },
        )
        plugin.record_worktree_mapping(self.context, self.state_dir)
        result, document, errors = self.invoke_json(
            ["remove-worktree", "--target", os.fspath(self.target), "--confirm", "feature"]
        )
        self.assertEqual(result, 0)
        self.assertTrue(document["ok"])
        self.assertEqual(document["worktree_path"], os.fspath(self.target.resolve()))
        self.assertEqual(document["repo_root"], os.fspath(self.source.resolve()))
        self.assertEqual(document["branch"], "feature")
        self.assertEqual(document["workspace_id"], "w2")
        self.assertFalse(document["forced"])
        self.assertEqual(document["removal"]["removal_operation_status"], "requested")
        self.assertEqual(document["removal"]["before_remove_status"], "success")
        self.assertIn("lifecycle-output", errors)
        removal_calls = [
            call for call in self.calls()
            if call["argv"][:2] == ["worktree", "remove"]
        ]
        self.assertEqual(len(removal_calls), 1)
        self.assertEqual(removal_calls[0]["argv"], ["worktree", "remove", "--workspace", "w2"])
        self.assertTrue(removal_calls[0]["before_marker_exists"])
        run(["git", "worktree", "remove", os.fspath(self.target)], cwd=self.source)
        with contextlib.redirect_stdout(io.StringIO()):
            pending = plugin.handle_branch_cleanup_event(
                dict(self.env, **self.removed_event()), self.state_dir, open_popup=False
            )
        self.assertEqual(document["branch_cleanup_id"], pending["id"])
        self.assertTrue(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())

    def test_branch_cleanup_list_includes_current_branch_and_leftover_state(self):
        pending = self.queue_removed_target()
        result, document, _ = self.invoke_json(["branch-cleanup", "--list"])
        self.assertEqual(result, 0)
        self.assertTrue(document["ok"])
        listed, = document["pending"]
        self.assertEqual(listed["id"], pending["id"])
        self.assertEqual(listed["repo_root"], os.fspath(self.source.resolve()))
        self.assertEqual(listed["branch"], "feature")
        self.assertTrue(listed["branch_state"]["exists"])
        self.assertFalse(listed["branch_state"]["protected"])
        self.assertTrue(listed["branch_state"]["merged_into_default"])
        self.assertEqual(listed["branch_state"]["used_by_worktrees"], [])
        self.assertIsNone(listed["inspection_error"])
        self.assertFalse(listed["leftover"]["exists"])
        self.assertIsNone(listed["branch_outcome"])
        self.assertIsNone(listed["checkout_outcome"])
        self.assertTrue(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())

    def test_branch_cleanup_keep_resolves_request_and_writes_last_result(self):
        pending = self.queue_removed_target()
        result, document, _ = self.invoke_json(
            self.cleanup_arguments(pending, "--decision", "keep")
        )
        self.assertEqual(result, 0)
        self.assertTrue(document["ok"])
        self.assertTrue(document["resolved"])
        self.assertEqual(document["id"], pending["id"])
        self.assertEqual(document["branch_outcome"], "kept")
        self.assertEqual(document["checkout_outcome"], "absent")
        self.assertIsNone(document["needs"])
        self.assertTrue(self.branch_exists("feature"))
        self.assertFalse(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())
        self.assertEqual(self.last_cleanup_result()["outcome"], "kept")

    def test_branch_cleanup_delete_requires_exact_branch_confirmation(self):
        pending = self.queue_removed_target()
        self.assert_cli_error(
            self.cleanup_arguments(pending, "--decision", "delete", "--confirm", "Feature"),
            "confirmation_mismatch",
        )
        self.assertTrue(self.branch_exists("feature"))
        self.assertTrue(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())

    def test_branch_cleanup_deletes_merged_branch_safely(self):
        pending = self.queue_removed_target()
        result, document, _ = self.invoke_json(
            self.cleanup_arguments(pending, "--decision", "delete", "--confirm", "feature")
        )
        self.assertEqual(result, 0)
        self.assertTrue(document["ok"])
        self.assertTrue(document["resolved"])
        self.assertEqual(document["branch_outcome"], "deleted_safely")
        self.assertFalse(self.branch_exists("feature"))
        self.assertFalse(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())
        self.assertEqual(self.last_cleanup_result()["outcome"], "deleted_safely")

    def test_branch_cleanup_refuses_unmerged_delete_then_accepts_confirmed_force(self):
        (self.target / "feature-only").write_text("unmerged", encoding="utf-8")
        run(["git", "add", "feature-only"], cwd=self.target)
        run(["git", "commit", "-qm", "feature only"], cwd=self.target)
        pending = self.queue_removed_target()
        self.assert_cli_error(
            self.cleanup_arguments(pending, "--decision", "delete", "--confirm", "feature"),
            "safe_delete_refused",
        )
        self.assertTrue(self.branch_exists("feature"))
        self.assertTrue(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())
        self.assertEqual(self.last_cleanup_result()["outcome"], "safe_delete_failed")
        self.assert_cli_error(
            self.cleanup_arguments(pending, "--decision", "force", "--confirm", "wrong"),
            "confirmation_mismatch",
        )
        self.assertTrue(self.branch_exists("feature"))
        result, document, _ = self.invoke_json(
            self.cleanup_arguments(pending, "--decision", "force", "--confirm", "feature")
        )
        self.assertEqual(result, 0)
        self.assertTrue(document["ok"])
        self.assertTrue(document["resolved"])
        self.assertEqual(document["branch_outcome"], "deleted_forcibly")
        self.assertFalse(self.branch_exists("feature"))
        self.assertFalse(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())
        self.assertEqual(self.last_cleanup_result()["outcome"], "deleted_forcibly")

    def test_branch_cleanup_never_deletes_protected_branch(self):
        pending = self.queue_removed_target(branch="main")
        self.assert_cli_error(
            self.cleanup_arguments(pending, "--decision", "force", "--confirm", "main"),
            "branch_protected",
        )
        self.assertTrue(self.branch_exists("main"))
        self.assertTrue((self.source / "tracked.txt").exists())
        self.assertTrue(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())

    def test_branch_cleanup_persists_branch_decision_before_later_checkout_keep(self):
        pending = self.queue_removed_target(leftover=True)
        result, document, _ = self.invoke_json(
            self.cleanup_arguments(pending, "--decision", "delete", "--confirm", "feature")
        )
        self.assertEqual(result, 0)
        self.assertTrue(document["ok"])
        self.assertFalse(document["resolved"])
        self.assertEqual(document["needs"], "checkout_decision")
        self.assertEqual(document["branch_outcome"], "deleted_safely")
        self.assertIsNone(document["checkout_outcome"])
        self.assertFalse(self.branch_exists("feature"))
        saved = json.loads(
            plugin.pending_cleanup_path(self.state_dir, pending["id"]).read_text(encoding="utf-8")
        )
        self.assertEqual(saved["branch_outcome"], "deleted_safely")
        result, document, _ = self.invoke_json(
            self.cleanup_arguments(pending, "--checkout", "keep")
        )
        self.assertEqual(result, 0)
        self.assertTrue(document["ok"])
        self.assertTrue(document["resolved"])
        self.assertEqual(document["branch_outcome"], "deleted_safely")
        self.assertEqual(document["checkout_outcome"], "kept")
        self.assertTrue((self.target / "ios" / "cache").exists())
        self.assertFalse(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())
        self.assertEqual(self.last_cleanup_result()["outcome"], "kept_with_leftover_files")

    def test_branch_cleanup_removes_confirmed_leftover_under_configured_root(self):
        pending = self.queue_removed_target(leftover=True)
        self.assert_cli_error(
            self.cleanup_arguments(
                pending, "--decision", "keep", "--checkout", "remove",
                "--confirm-checkout", "wrong",
            ),
            "confirmation_mismatch",
        )
        self.assertTrue((self.target / "ios" / "cache").exists())
        saved = json.loads(
            plugin.pending_cleanup_path(self.state_dir, pending["id"]).read_text(encoding="utf-8")
        )
        self.assertIsNone(saved.get("branch_outcome"))
        result, document, _ = self.invoke_json(
            self.cleanup_arguments(
                pending, "--decision", "keep", "--checkout", "remove",
                "--confirm-checkout", self.target.name,
            )
        )
        self.assertEqual(result, 0)
        self.assertTrue(document["ok"])
        self.assertTrue(document["resolved"])
        self.assertEqual(document["branch_outcome"], "kept")
        self.assertEqual(document["checkout_outcome"], "removed")
        self.assertFalse(self.target.exists())
        self.assertTrue(self.branch_exists("feature"))
        self.assertFalse(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())

    def test_branch_cleanup_rejects_checkout_confirmation_before_deleting_branch(self):
        pending = self.queue_removed_target(leftover=True)
        self.assertTrue(plugin.inspect_branch(self.source, "feature").merged_into_default)
        self.assert_cli_error(
            self.cleanup_arguments(
                pending, "--decision", "delete", "--confirm", "feature",
                "--checkout", "remove", "--confirm-checkout", "wrong",
            ),
            "confirmation_mismatch",
        )
        self.assertTrue(self.branch_exists("feature"))
        self.assertTrue((self.target / "ios" / "cache").exists())
        saved = json.loads(
            plugin.pending_cleanup_path(self.state_dir, pending["id"]).read_text(encoding="utf-8")
        )
        self.assertIsNone(saved.get("branch_outcome"))

    def test_branch_cleanup_requires_id_for_noninteractive_decision(self):
        self.assert_cli_error(
            ["branch-cleanup", "--decision", "keep"],
            "cleanup_id_required",
        )

    def test_branch_cleanup_unknown_id_is_structured_error(self):
        self.assert_cli_error(
            ["branch-cleanup", "--id", "a" * 24, "--decision", "keep"],
            "cleanup_not_found",
        )

    def test_branch_cleanup_accepts_id_from_event_environment(self):
        pending = self.queue_removed_target()
        result, document, _ = self.invoke_json(
            ["branch-cleanup", "--decision", "keep"],
            env={"HERDR_BRANCH_CLEANUP_ID": pending["id"]},
        )
        self.assertEqual(result, 0)
        self.assertTrue(document["ok"])
        self.assertTrue(document["resolved"])
        self.assertEqual(document["id"], pending["id"])
        self.assertFalse(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())

    def review_cleanup(self, pending, input_fn):
        return plugin.review_pending_cleanups(
            self.state_dir, cleanup_id=pending["id"], input_fn=input_fn,
            output_fn=lambda line: None, env=self.env,
        )

    def saved_pending(self, pending):
        return json.loads(
            plugin.pending_cleanup_path(self.state_dir, pending["id"]).read_text(encoding="utf-8")
        )

    def receive_cleanup_worker(self, connection):
        self.assertTrue(connection.poll(15), "cleanup worker did not reach its barrier")
        return connection.recv()

    @contextlib.contextmanager
    def contending_cleanup_workers(self, pending, *, checkout=False):
        context = multiprocessing.get_context("spawn")
        children = []
        connections = []

        def start(holder):
            parent, child = context.Pipe()
            connections.extend((parent, child))
            process = context.Process(
                target=cleanup_decision_worker,
                args=(child, os.fspath(self.state_dir), pending["id"], self.env),
                kwargs={"holder": holder, "checkout": checkout},
            )
            process.start()
            children.append(process)
            child.close()
            return parent

        try:
            holder = start(True)
            self.assertEqual(self.receive_cleanup_worker(holder), "git-mutation")
            contender = start(False)
            self.assertEqual(self.receive_cleanup_worker(contender), "contended")
            yield holder, contender
            for process in children:
                process.join(15)
                self.assertEqual(process.exitcode, 0, "cleanup worker did not exit successfully")
        finally:
            for connection in connections:
                connection.close()
            for process in children:
                if process.is_alive():
                    process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join(5)
                process.close()

    def test_branch_helper_reports_actual_safe_force_and_absent_outcomes(self):
        self.record_and_remove_target()
        oid = run(["git", "rev-parse", "feature"], cwd=self.source).stdout.strip()
        for force, outcome in ((False, "deleted_safely"), (True, "deleted_forcibly")):
            with self.subTest(force=force):
                if not self.branch_exists("feature"):
                    run(["git", "branch", "feature", oid], cwd=self.source)
                self.assertEqual(
                    plugin.delete_local_branch(self.source, "feature", force=force), outcome
                )
                self.assertFalse(self.branch_exists("feature"))
                self.assertEqual(
                    plugin.delete_local_branch(self.source, "feature", force=force),
                    "already_absent",
                )

    def test_cli_and_ui_helper_races_preserve_truthful_absence(self):
        pending = self.queue_removed_target()
        oid = run(["git", "rev-parse", "feature"], cwd=self.source).stdout.strip()
        original = plugin.delete_local_branch

        def remove_before_helper(*args, **kwargs):
            run(["git", "branch", "-D", "feature"], cwd=self.source)
            return original(*args, **kwargs)

        for adapter in ("cli", "ui"):
            for force in (False, True):
                with self.subTest(adapter=adapter, force=force):
                    if not self.branch_exists("feature"):
                        run(["git", "branch", "feature", oid], cwd=self.source)
                        pending = plugin.queue_branch_cleanup(self.removed_event(), self.state_dir)
                    with mock.patch.object(plugin, "delete_local_branch", side_effect=remove_before_helper):
                        if adapter == "cli":
                            status, document, _ = self.invoke_json(self.cleanup_arguments(
                                pending, "--decision", "force" if force else "delete",
                                "--confirm", "feature",
                            ))
                            self.assertEqual(status, 0)
                            self.assertEqual(document["branch_outcome"], "already_absent")
                        else:
                            answers = iter(["f", "feature"] if force else ["d"])
                            self.assertEqual(
                                self.review_cleanup(pending, lambda prompt: next(answers)), 0
                            )
                    self.assertFalse(self.branch_exists("feature"))
                    self.assertFalse(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())
                    self.assertEqual(self.last_cleanup_result()["branch_outcome"], "already_absent")
                    self.assertEqual(self.last_cleanup_result()["outcome"], "already_absent")

    def test_git_command_deletion_race_is_absent_not_a_plugin_deletion(self):
        pending = self.queue_removed_target()
        oid = run(["git", "rev-parse", "feature"], cwd=self.source).stdout.strip()
        original = plugin._run_git

        def race_at_git(repo, arguments, **kwargs):
            if arguments[:2] in (["branch", "-d"], ["branch", "-D"]):
                run(["git", "branch", "-D", "feature"], cwd=self.source)
            return original(repo, arguments, **kwargs)

        for adapter in ("cli", "ui"):
            for force in (False, True):
                with self.subTest(adapter=adapter, force=force):
                    if not self.branch_exists("feature"):
                        run(["git", "branch", "feature", oid], cwd=self.source)
                        pending = plugin.queue_branch_cleanup(self.removed_event(), self.state_dir)
                    with mock.patch.object(plugin, "_run_git", side_effect=race_at_git):
                        if adapter == "cli":
                            status, document, _ = self.invoke_json(self.cleanup_arguments(
                                pending, "--decision", "force" if force else "delete",
                                "--confirm", "feature",
                            ))
                            self.assertEqual(status, 0)
                            self.assertEqual(document["branch_outcome"], "already_absent")
                        else:
                            answers = iter(["f", "feature"] if force else ["d"])
                            self.review_cleanup(pending, lambda prompt: next(answers))
                    self.assertEqual(self.last_cleanup_result()["branch_outcome"], "already_absent")
                    self.assertEqual(self.last_cleanup_result()["outcome"], "already_absent")
                    self.assertFalse(self.branch_exists("feature"))
                    self.assertFalse(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())

    def test_real_git_failure_keeps_branch_and_pending_for_both_adapters(self):
        pending = self.queue_removed_target()
        ref_lock = self.source / ".git" / "refs" / "heads" / "feature.lock"
        ref_lock.write_text("fixture lock\n", encoding="utf-8")
        for adapter in ("cli", "ui"):
            for force in (False, True):
                with self.subTest(adapter=adapter, force=force):
                    if adapter == "cli":
                        self.assert_cli_error(
                            self.cleanup_arguments(
                                pending, "--decision", "force" if force else "delete",
                                "--confirm", "feature",
                            ),
                            "force_delete_failed" if force else "safe_delete_refused",
                        )
                    else:
                        answers = iter(["f", "feature", "q"] if force else ["d", "q"])
                        self.review_cleanup(pending, lambda prompt: next(answers))
                    self.assertTrue(self.branch_exists("feature"))
                    self.assertIsNone(self.saved_pending(pending).get("branch_outcome"))
                    self.assertEqual(
                        self.last_cleanup_result()["outcome"],
                        "force_delete_failed" if force else "safe_delete_failed",
                    )
        self.assertEqual(ref_lock.read_text(encoding="utf-8"), "fixture lock\n")

    def test_ui_rechecks_the_displayed_oid_before_safe_or_force_deletion(self):
        pending = self.queue_removed_target()
        tree = run(["git", "rev-parse", "HEAD^{tree}"], cwd=self.source).stdout.strip()
        advanced = run(
            ["git", "commit-tree", tree, "-p", "HEAD", "-m", "new feature tip"],
            cwd=self.source,
        ).stdout.strip()
        original_oid = run(["git", "rev-parse", "feature"], cwd=self.source).stdout.strip()
        for force in (False, True):
            with self.subTest(force=force):
                run(["git", "update-ref", "refs/heads/feature", original_oid], cwd=self.source)
                answers = iter(["f", "feature", "q"] if force else ["d", "q"])

                def answer(prompt):
                    value = next(answers)
                    if value == ("feature" if force else "d"):
                        run(["git", "update-ref", "refs/heads/feature", advanced], cwd=self.source)
                    return value

                self.review_cleanup(pending, answer)
                self.assertEqual(
                    run(["git", "rev-parse", "feature"], cwd=self.source).stdout.strip(),
                    advanced,
                )
                self.assertIsNone(self.saved_pending(pending).get("branch_outcome"))

    def test_ui_rechecks_real_directory_inode_after_typed_confirmation(self):
        pending = self.queue_removed_target(leftover=True)
        old = self.root / "original residue"
        old_inode = self.target.stat().st_ino
        answers = iter(["k", "r", self.target.name, "q"])

        def answer(prompt):
            value = next(answers)
            if value == self.target.name:
                self.target.rename(old)
                self.target.mkdir()
                (self.target / "replacement").write_text("must survive", encoding="utf-8")
                self.assertNotEqual(self.target.stat().st_ino, old_inode)
            return value

        self.review_cleanup(pending, answer)
        self.assertEqual((self.target / "replacement").read_text(encoding="utf-8"), "must survive")
        self.assertTrue((old / "ios" / "cache").exists())
        self.assertEqual(self.saved_pending(pending)["branch_outcome"], "kept")
        self.assertIsNone(self.saved_pending(pending).get("checkout_outcome"))

    def test_cli_checkout_inode_race_preserves_saved_branch_progress(self):
        pending = self.queue_removed_target(leftover=True)
        original = plugin.remove_leftover_worktree_files
        old = self.root / "original residue"

        def replace_before_remove(*args, **kwargs):
            self.target.rename(old)
            self.target.mkdir()
            (self.target / "replacement").write_text("must survive", encoding="utf-8")
            return original(*args, **kwargs)

        with mock.patch.object(
            plugin, "remove_leftover_worktree_files", side_effect=replace_before_remove
        ):
            self.assert_cli_error(self.cleanup_arguments(
                pending, "--decision", "delete", "--confirm", "feature",
                "--checkout", "remove", "--confirm-checkout", self.target.name,
            ), "checkout_delete_failed")
        self.assertFalse(self.branch_exists("feature"))
        self.assertTrue((self.target / "replacement").exists())
        self.assertTrue((old / "ios" / "cache").exists())
        self.assertEqual(self.saved_pending(pending)["branch_outcome"], "deleted_safely")
        self.assertIsNone(self.saved_pending(pending).get("checkout_outcome"))

    def test_selected_invalid_or_unreadable_cleanup_reports_refusal_without_mutation(self):
        pending = self.queue_removed_target()
        path = plugin.pending_cleanup_path(self.state_dir, pending["id"])
        original = path.read_text(encoding="utf-8")
        without_id = dict(pending)
        without_id.pop("id")
        original_read = Path.read_text

        def unreadable(selected, *args, **kwargs):
            if selected == path:
                raise PermissionError("fixture pending record is unreadable")
            return original_read(selected, *args, **kwargs)

        for reason, contents in (
            ("malformed-json", "{"),
            ("missing-id", json.dumps(without_id)),
            ("unreadable", original),
        ):
            with self.subTest(reason=reason):
                path.write_text(contents, encoding="utf-8")
                messages = []
                patch = (
                    mock.patch.object(Path, "read_text", new=unreadable)
                    if reason == "unreadable" else contextlib.nullcontext()
                )
                with patch:
                    self.assertEqual(plugin.review_pending_cleanups(
                        self.state_dir, cleanup_id=pending["id"],
                        input_fn=mock.Mock(side_effect=AssertionError("invalid request prompted")),
                        output_fn=messages.append, env=self.env,
                    ), 0)
                    self.assert_cli_error(
                        self.cleanup_arguments(pending, "--decision", "keep"), "invalid_record"
                    )
                self.assertTrue(messages, "invalid selected request was silently ignored")
                self.assertTrue(all(pending["id"] in message for message in messages))
                self.assertEqual(path.read_text(encoding="utf-8"), contents)
                self.assertTrue(self.branch_exists("feature"))
                self.assertFalse(self.target.exists())
                self.assertFalse(
                    (self.state_dir / plugin.BRANCH_CLEANUP_DIR / "last-result.json").exists()
                )

    def test_review_all_reports_invalid_requests_and_completes_valid_request(self):
        pending = self.queue_removed_target()
        without_id = dict(pending)
        without_id.pop("id")
        invalid = {
            "a" * 24: "{",
            "b" * 24: json.dumps(without_id),
        }
        for cleanup_id, contents in invalid.items():
            plugin.pending_cleanup_path(self.state_dir, cleanup_id).write_text(
                contents, encoding="utf-8"
            )
        answers = iter(["k"])
        messages = []
        self.assertEqual(plugin.review_pending_cleanups(
            self.state_dir, input_fn=lambda prompt: next(answers),
            output_fn=messages.append, env=self.env,
        ), 0)
        for cleanup_id, contents in invalid.items():
            self.assertTrue(any(cleanup_id in message for message in messages))
            self.assertEqual(
                plugin.pending_cleanup_path(self.state_dir, cleanup_id).read_text(encoding="utf-8"),
                contents,
            )
        self.assertFalse(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())
        self.assertTrue(self.branch_exists("feature"))
        self.assertFalse(self.target.exists())
        final = self.last_cleanup_result()
        self.assertEqual(final["branch_outcome"], "kept")
        self.assertEqual(final["checkout_outcome"], "absent")
        self.assertEqual(final["outcome"], "kept")

    def test_same_id_process_decisions_cannot_resurrect_or_overwrite_winner(self):
        pending = self.queue_removed_target()
        with self.contending_cleanup_workers(pending) as (holder, contender):
            self.assertTrue(self.branch_exists("feature"))
            self.assertTrue(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())
            holder.send("continue")
            kind, winner = self.receive_cleanup_worker(holder)
            self.assertEqual(kind, "result")
            self.assertTrue(winner["resolved"])
            self.assertEqual(winner["branch_outcome"], "deleted_safely")
            self.assertEqual(winner["checkout_outcome"], "absent")
            self.assertEqual(
                self.receive_cleanup_worker(contender), ("error", "cleanup_not_found")
            )
        self.assertFalse(self.branch_exists("feature"))
        self.assertFalse(self.target.exists())
        self.assertFalse(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())
        final = self.last_cleanup_result()
        self.assertEqual(final["branch_outcome"], "deleted_safely")
        self.assertEqual(final["checkout_outcome"], "absent")
        self.assertEqual(final["outcome"], "deleted_safely")

    def test_same_id_process_checkout_completion_preserves_saved_branch_progress(self):
        pending = self.queue_removed_target(leftover=True)
        with self.contending_cleanup_workers(pending, checkout=True) as (holder, contender):
            holder.send("continue")
            kind, partial = self.receive_cleanup_worker(holder)
            self.assertEqual(kind, "result")
            self.assertFalse(partial["resolved"])
            self.assertEqual(partial["branch_outcome"], "deleted_safely")
            self.assertEqual(self.receive_cleanup_worker(contender), "checkout-mutation")
            self.assertFalse(self.branch_exists("feature"))
            self.assertEqual(
                (self.target / "ios" / "cache").read_text(encoding="utf-8"), "generated"
            )
            saved = self.saved_pending(pending)
            self.assertEqual(saved["branch_outcome"], "deleted_safely")
            self.assertIsNone(saved.get("checkout_outcome"))
            contender.send("continue")
            kind, completed = self.receive_cleanup_worker(contender)
            self.assertEqual(kind, "result")
            self.assertTrue(completed["resolved"])
            self.assertEqual(completed["branch_outcome"], "deleted_safely")
            self.assertEqual(completed["checkout_outcome"], "removed")
        self.assertFalse(self.branch_exists("feature"))
        self.assertFalse(self.target.exists())
        self.assertFalse(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())
        final = self.last_cleanup_result()
        self.assertEqual(final["branch_outcome"], "deleted_safely")
        self.assertEqual(final["checkout_outcome"], "removed")
        self.assertEqual(final["outcome"], "deleted_safely")

    def test_concurrent_resolution_during_ui_input_never_resurrects_request(self):
        pending = self.queue_removed_target()
        winner = []

        def answer(prompt):
            result = plugin.decide_pending_cleanup(
                self.state_dir, cleanup_id=pending["id"], decision="keep", env=self.env
            )
            self.assertTrue(result["resolved"])
            winner.append(self.last_cleanup_result())
            return "d"

        self.assertEqual(self.review_cleanup(pending, answer), 0)
        self.assertTrue(self.branch_exists("feature"))
        self.assertEqual(self.last_cleanup_result(), winner[0])
        self.assertFalse(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())

    def test_concurrent_partial_branch_decision_refreshes_before_checkout_choice(self):
        pending = self.queue_removed_target(leftover=True)
        answers = iter(["d", "r", self.target.name])

        def answer(prompt):
            value = next(answers)
            if value == "d":
                result = plugin.decide_pending_cleanup(
                    self.state_dir, cleanup_id=pending["id"], decision="keep", env=self.env
                )
                self.assertFalse(result["resolved"])
                self.assertEqual(result["branch_outcome"], "kept")
            return value

        self.review_cleanup(pending, answer)
        self.assertTrue(self.branch_exists("feature"))
        self.assertFalse(self.target.exists())
        self.assertEqual(self.last_cleanup_result()["branch_outcome"], "kept")
        self.assertEqual(self.last_cleanup_result()["checkout_outcome"], "removed")
        self.assertEqual(plugin.list_pending_cleanups(self.state_dir), [])

    def test_duplicate_queue_preserves_generation_and_partial_progress(self):
        pending = self.queue_removed_target(leftover=True)
        result = plugin.decide_pending_cleanup(
            self.state_dir, cleanup_id=pending["id"], decision="delete",
            confirm="feature", env=self.env,
        )
        self.assertFalse(result["resolved"])
        saved = self.saved_pending(pending)
        duplicate = plugin.queue_branch_cleanup(self.removed_event(), self.state_dir)
        self.assertEqual(duplicate["id"], pending["id"])
        refreshed = self.saved_pending(pending)
        for key in ("id", "created_at", "repo_root", "branch", "worktree_path",
                    "branch_outcome", "checkout_outcome"):
            self.assertEqual(refreshed.get(key), saved.get(key), key)
        result = plugin.decide_pending_cleanup(
            self.state_dir, cleanup_id=pending["id"], checkout="keep", env=self.env
        )
        self.assertEqual(result["branch_outcome"], "deleted_safely")
        self.assertEqual(result["checkout_outcome"], "kept")
        self.assertTrue(result["resolved"])

    def test_requeued_same_id_rejects_stale_ui_approval(self):
        pending = self.queue_removed_target()
        replacement = []
        winner = []

        def answer(prompt):
            if not replacement:
                plugin.decide_pending_cleanup(
                    self.state_dir, cleanup_id=pending["id"], decision="keep", env=self.env
                )
                winner.append(self.last_cleanup_result())
                replacement.append(plugin.queue_branch_cleanup(self.removed_event(), self.state_dir))
                self.assertEqual(replacement[0]["id"], pending["id"])
                self.assertNotEqual(replacement[0]["created_at"], pending["created_at"])
                return "d"
            return "q"

        self.review_cleanup(pending, answer)
        self.assertTrue(self.branch_exists("feature"))
        self.assertEqual(self.saved_pending(pending)["created_at"], replacement[0]["created_at"])
        self.assertIsNone(self.saved_pending(pending).get("branch_outcome"))
        self.assertEqual(self.last_cleanup_result(), winner[0])

    def test_resolving_selected_request_does_not_modify_another_id(self):
        pending = self.queue_removed_target()
        run(["git", "branch", "other-feature"], cwd=self.source)
        other = plugin.queue_branch_cleanup(self.removed_event(branch="other-feature"), self.state_dir)
        saved_other = self.saved_pending(other)
        plugin.decide_pending_cleanup(
            self.state_dir, cleanup_id=pending["id"], decision="keep", env=self.env
        )
        self.assertEqual(self.saved_pending(other), saved_other)
        self.assertTrue(self.branch_exists("other-feature"))
        self.assertEqual([record["id"] for record in plugin.list_pending_cleanups(self.state_dir)], [other["id"]])

    def test_raw_idle_absence_and_external_completion_exit_without_key(self):
        pending = self.queue_removed_target()
        oid = run(["git", "rev-parse", "feature"], cwd=self.source).stdout.strip()
        for external in ("branch", "request"):
            with self.subTest(external=external):
                if not self.branch_exists("feature"):
                    run(["git", "branch", "feature", oid], cwd=self.source)
                if not plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists():
                    pending = plugin.queue_branch_cleanup(self.removed_event(), self.state_dir)
                winner = []

                def timeout(**kwargs):
                    self.assertEqual(kwargs.get("timeout_seconds"), 1.0)
                    if external == "branch":
                        run(["git", "branch", "-D", "feature"], cwd=self.source)
                    else:
                        plugin.decide_pending_cleanup(
                            self.state_dir, cleanup_id=pending["id"], decision="keep", env=self.env
                        )
                        winner.append(self.last_cleanup_result())
                    return None

                with mock.patch.object(plugin.sys.stdin, "isatty", return_value=True):
                    with mock.patch.object(plugin, "read_single_key", side_effect=timeout) as keys:
                        result = plugin.review_pending_cleanups(
                            self.state_dir, cleanup_id=pending["id"],
                            output_fn=lambda line: None, env=self.env,
                        )
                self.assertEqual(result, 0)
                self.assertEqual(keys.call_count, 1)
                self.assertFalse(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())
                if winner:
                    self.assertEqual(self.last_cleanup_result(), winner[0])
                    self.assertTrue(self.branch_exists("feature"))
                else:
                    self.assertEqual(self.last_cleanup_result()["branch_outcome"], "already_absent")

    def test_raw_idle_absence_with_residue_still_requires_checkout_decision(self):
        pending = self.queue_removed_target(leftover=True)
        calls = []

        def key(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                run(["git", "branch", "-D", "feature"], cwd=self.source)
                return None
            self.assertEqual(self.saved_pending(pending)["branch_outcome"], "already_absent")
            self.assertTrue((self.target / "ios" / "cache").exists())
            return "q"

        with mock.patch.object(plugin.sys.stdin, "isatty", return_value=True):
            with mock.patch.object(plugin, "read_single_key", side_effect=key):
                plugin.review_pending_cleanups(
                    self.state_dir, cleanup_id=pending["id"],
                    output_fn=lambda line: None, env=self.env,
                )
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.saved_pending(pending)["branch_outcome"], "already_absent")
        self.assertIsNone(self.saved_pending(pending).get("checkout_outcome"))
        self.assertTrue((self.target / "ios" / "cache").exists())

    def test_ui_eof_leaves_undecided_cleanup_pending(self):
        pending = self.queue_removed_target()
        self.assertEqual(self.review_cleanup(pending, mock.Mock(side_effect=EOFError)), 0)
        self.assertTrue(self.branch_exists("feature"))
        self.assertIsNone(self.saved_pending(pending).get("branch_outcome"))

    def test_inventory_returns_to_parent_after_nested_cleanup(self):
        pending = self.queue_removed_target()
        answers = iter(["b", "d", "q"])
        observed = []

        def answer(prompt):
            value = next(answers)
            if value == "q":
                self.assertFalse(self.branch_exists("feature"))
                self.assertFalse(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())
                self.assertEqual(self.last_cleanup_result()["branch_outcome"], "deleted_safely")
            observed.append(value)
            return value

        result = plugin.manage_worktree_inventory(
            plugin.resolve_repository(self.source), self.state_dir,
            env=self.env, input_fn=answer, output_fn=lambda line: None,
        )
        self.assertEqual(result, 0)
        self.assertEqual(observed, ["b", "d", "q"])

    def test_raw_timeout_without_change_does_not_decide_or_delete(self):
        pending = self.queue_removed_target(leftover=True)
        before = self.saved_pending(pending)
        with mock.patch.object(plugin.sys.stdin, "isatty", return_value=True):
            with mock.patch.object(plugin, "read_single_key", side_effect=[None, "q"]):
                plugin.review_pending_cleanups(
                    self.state_dir, cleanup_id=pending["id"],
                    output_fn=lambda line: None, env=self.env,
                )
        self.assertEqual(self.saved_pending(pending), before)
        self.assertTrue(self.branch_exists("feature"))
        self.assertTrue((self.target / "ios" / "cache").exists())

    def test_listing_absent_branch_is_observational_until_review(self):
        pending = self.queue_removed_target()
        run(["git", "branch", "-D", "feature"], cwd=self.source)
        before = self.saved_pending(pending)
        status, document, _ = self.invoke_json(["branch-cleanup", "--list"])
        self.assertEqual(status, 0)
        listed, = document["pending"]
        self.assertFalse(listed["branch_state"]["exists"])
        self.assertIsNone(listed["branch_outcome"])
        self.assertEqual(self.saved_pending(pending), before)

    def test_inventory_residue_removal_reconciles_matching_pending_and_returns(self):
        pending = self.queue_removed_target(leftover=True)
        partial = plugin.decide_pending_cleanup(
            self.state_dir, cleanup_id=pending["id"], decision="keep", env=self.env
        )
        self.assertFalse(partial["resolved"])
        answers = iter(["c", "1", self.target.name, "q"])
        observed = []

        def answer(prompt):
            value = next(answers)
            if value == "q":
                self.assertFalse(self.target.exists())
                self.assertFalse(plugin.pending_cleanup_path(self.state_dir, pending["id"]).exists())
                self.assertEqual(self.last_cleanup_result()["branch_outcome"], "kept")
            observed.append(value)
            return value

        self.assertEqual(plugin.manage_worktree_inventory(
            plugin.resolve_repository(self.source), self.state_dir, env=self.env,
            input_fn=answer, output_fn=lambda line: None,
        ), 0)
        self.assertEqual(observed, ["c", "1", self.target.name, "q"])
        self.assertTrue(self.branch_exists("feature"))


if __name__ == "__main__":
    unittest.main()
