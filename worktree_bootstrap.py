#!/usr/bin/env python3
"""Herdr worktree bootstrap plugin.

The module intentionally uses only the Python 3.9 standard library.  Its public
functions are kept small enough to exercise directly from unittest while the
CLI remains the single entrypoint used by Herdr actions and event hooks.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import itertools
import json
import os
from pathlib import Path, PurePosixPath
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import termios
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
import unicodedata
import uuid


PLUGIN_ID = "zerodice0.worktree-bootstrap"
COPY_LIST = Path(".herdr/worktree-copy.list")
SETUP_FILE = Path(".herdr/worktree-setup.json")
TXN_PREFIX = ".herdr-worktree-bootstrap-txn-"
BRANCH_CLEANUP_DIR = "branch-cleanup"
WORKTREE_MAP_DIR = "worktree-map"
PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "development", "trunk"})
LIFECYCLE_PHASES = ("on_created", "before_remove", "after_remove")
LEFTOVER_SAMPLE_LIMIT = 8
LEFTOVER_ENTRY_LIMIT = 10_000
ANSI_RESET = "\033[0m"
MIN_ACTION_FOOTER_COLUMNS = 32
ACTION_GROUP_GAP = 4
MANAGE_POPUP_SIZE = ("96", "24")
SETUP_MANAGE_POPUP_SIZE = ("84", "20")
INVENTORY_POPUP_SIZE = ("104", "30")
ColorValue = Optional[Union[int, Tuple[int, int, int]]]
ActionSpec = Tuple[str, str, str]


class BootstrapError(RuntimeError):
    """A user-facing validation or execution failure."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class SetupCommandError(BootstrapError):
    """A setup failure with safe structured metadata for the last-run record."""

    def __init__(
        self,
        message: str,
        *,
        command_index: int,
        command_name: str,
        failure_kind: str,
        command_exit_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.command_index = command_index
        self.command_name = command_name
        self.failure_kind = failure_kind
        self.command_exit_code = command_exit_code


class NothingToDo(RuntimeError):
    """A safe no-op condition which should exit successfully."""


class NotGitRepository(NothingToDo):
    """The selected workspace is not backed by a Git repository."""


@dataclasses.dataclass(frozen=True)
class RepositoryContext:
    target: Path
    source: Path
    common_git_dir: Path
    target_is_primary: bool


@dataclasses.dataclass(frozen=True)
class CopyEntryStatus:
    path: str
    source_exists: bool
    target_exists: bool
    eligibility: str
    detail: str

    @property
    def copyable(self) -> bool:
        return self.eligibility == "ignored" and self.source_exists


@dataclasses.dataclass(frozen=True)
class SetupCommand:
    name: str
    argv: Tuple[str, ...]
    timeout_seconds: int


@dataclasses.dataclass(frozen=True)
class BranchInspection:
    repo_root: Path
    branch: str
    exists: bool
    protected: bool
    protection_reason: Optional[str]
    used_by_worktrees: Tuple[str, ...]
    default_ref: Optional[str]
    merged_into_default: Optional[bool]
    upstream: Optional[str]
    ahead: Optional[int]
    behind: Optional[int]
    last_commit: Optional[str]
    head_oid: Optional[str]


@dataclasses.dataclass(frozen=True)
class LeftoverInspection:
    path: Path
    exists: bool
    is_directory: bool
    is_symlink: bool
    entry_count: int
    total_bytes: int
    sample_paths: Tuple[str, ...]
    truncated: bool
    device: Optional[int]
    inode: Optional[int]


@dataclasses.dataclass(frozen=True)
class WorktreeInventoryItem:
    repo_root: Path
    worktree_path: Path
    branch: Optional[str]
    head_oid: Optional[str]
    created_head_oid: Optional[str]
    base_candidates: Tuple[str, ...]
    registered: bool
    path_exists: bool
    branch_exists: Optional[bool]
    status: str
    provenance_verified: bool
    workspace_id: Optional[str]
    forced_removal: bool
    removed_at: Optional[str]
    on_created_status: Optional[str]
    before_remove_status: Optional[str]
    after_remove_status: Optional[str]
    leftover: LeftoverInspection


@dataclasses.dataclass(frozen=True)
class ThemeSettings:
    name: str = "catppuccin"
    auto_switch: bool = False
    dark_name: Optional[str] = None
    light_name: Optional[str] = None
    custom: Mapping[str, str] = dataclasses.field(default_factory=dict)
    legacy_accent: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class ThemePalette:
    name: str
    accent: ColorValue
    text: ColorValue
    muted: ColorValue
    subtle: ColorValue
    branch: ColorValue
    positive: ColorValue
    warning: ColorValue
    danger: ColorValue
    info: ColorValue
    peach: ColorValue


def _rgb(red: int, green: int, blue: int) -> Tuple[int, int, int]:
    return red, green, blue


# Semantic colors mirror Herdr 0.7.5's built-in Palette definitions.  The
# dialog deliberately leaves its background at the terminal default so the
# session-modal popup keeps the active client's background and border chrome.
HERDR_THEME_PALETTES: Mapping[str, ThemePalette] = {
    "catppuccin": ThemePalette(
        "catppuccin", _rgb(137, 180, 250), _rgb(205, 214, 244),
        _rgb(166, 173, 200), _rgb(127, 132, 156), _rgb(203, 166, 247),
        _rgb(166, 227, 161), _rgb(249, 226, 175), _rgb(243, 139, 168),
        _rgb(137, 180, 250), _rgb(250, 179, 135),
    ),
    "catppuccin-latte": ThemePalette(
        "catppuccin-latte", _rgb(30, 102, 245), _rgb(76, 79, 105),
        _rgb(108, 111, 133), _rgb(140, 143, 161), _rgb(136, 57, 239),
        _rgb(64, 160, 43), _rgb(223, 142, 29), _rgb(210, 15, 57),
        _rgb(30, 102, 245), _rgb(254, 100, 11),
    ),
    "terminal": ThemePalette(
        "terminal", 34, None, 90, 37, 35, 32, 33, 91, 34, 33,
    ),
    "tokyo-night": ThemePalette(
        "tokyo-night", _rgb(122, 162, 247), _rgb(192, 202, 245),
        _rgb(169, 177, 214), _rgb(105, 113, 150), _rgb(187, 154, 247),
        _rgb(158, 206, 106), _rgb(224, 175, 104), _rgb(247, 118, 142),
        _rgb(122, 162, 247), _rgb(255, 158, 100),
    ),
    "tokyo-night-day": ThemePalette(
        "tokyo-night-day", _rgb(46, 125, 233), _rgb(55, 96, 191),
        _rgb(97, 114, 176), _rgb(104, 112, 154), _rgb(120, 71, 189),
        _rgb(88, 117, 57), _rgb(140, 108, 62), _rgb(245, 42, 101),
        _rgb(46, 125, 233), _rgb(177, 92, 0),
    ),
    "dracula": ThemePalette(
        "dracula", _rgb(189, 147, 249), _rgb(248, 248, 242),
        _rgb(210, 210, 220), _rgb(130, 140, 180), _rgb(255, 121, 198),
        _rgb(80, 250, 123), _rgb(241, 250, 140), _rgb(255, 85, 85),
        _rgb(139, 233, 253), _rgb(255, 184, 108),
    ),
    "nord": ThemePalette(
        "nord", _rgb(136, 192, 208), _rgb(236, 239, 244),
        _rgb(216, 222, 233), _rgb(100, 110, 130), _rgb(180, 142, 173),
        _rgb(163, 190, 140), _rgb(235, 203, 139), _rgb(191, 97, 106),
        _rgb(129, 161, 193), _rgb(208, 135, 112),
    ),
    "gruvbox": ThemePalette(
        "gruvbox", _rgb(215, 153, 33), _rgb(235, 219, 178),
        _rgb(213, 196, 161), _rgb(168, 153, 132), _rgb(211, 134, 155),
        _rgb(184, 187, 38), _rgb(250, 189, 47), _rgb(251, 73, 52),
        _rgb(131, 165, 152), _rgb(254, 128, 25),
    ),
    "gruvbox-light": ThemePalette(
        "gruvbox-light", _rgb(7, 102, 120), _rgb(60, 56, 54),
        _rgb(80, 73, 69), _rgb(124, 111, 100), _rgb(143, 63, 113),
        _rgb(121, 116, 14), _rgb(181, 118, 20), _rgb(157, 0, 6),
        _rgb(7, 102, 120), _rgb(175, 58, 3),
    ),
    "one-dark": ThemePalette(
        "one-dark", _rgb(97, 175, 239), _rgb(171, 178, 191),
        _rgb(150, 156, 168), _rgb(115, 122, 135), _rgb(198, 120, 221),
        _rgb(152, 195, 121), _rgb(229, 192, 123), _rgb(224, 108, 117),
        _rgb(97, 175, 239), _rgb(209, 154, 102),
    ),
    "one-light": ThemePalette(
        "one-light", _rgb(64, 120, 242), _rgb(56, 58, 66),
        _rgb(104, 107, 119), _rgb(104, 107, 119), _rgb(166, 38, 164),
        _rgb(80, 161, 79), _rgb(193, 132, 1), _rgb(228, 86, 73),
        _rgb(64, 120, 242), _rgb(152, 104, 1),
    ),
    "solarized": ThemePalette(
        "solarized", _rgb(38, 139, 210), _rgb(147, 161, 161),
        _rgb(131, 148, 150), _rgb(101, 123, 131), _rgb(211, 54, 130),
        _rgb(133, 153, 0), _rgb(181, 137, 0), _rgb(220, 50, 47),
        _rgb(38, 139, 210), _rgb(203, 75, 22),
    ),
    "solarized-light": ThemePalette(
        "solarized-light", _rgb(38, 139, 210), _rgb(101, 123, 131),
        _rgb(131, 148, 150), _rgb(88, 110, 117), _rgb(211, 54, 130),
        _rgb(133, 153, 0), _rgb(181, 137, 0), _rgb(220, 50, 47),
        _rgb(38, 139, 210), _rgb(203, 75, 22),
    ),
    "kanagawa": ThemePalette(
        "kanagawa", _rgb(126, 156, 216), _rgb(220, 215, 186),
        _rgb(200, 195, 170), _rgb(135, 134, 125), _rgb(149, 127, 184),
        _rgb(118, 148, 106), _rgb(192, 163, 110), _rgb(195, 64, 67),
        _rgb(126, 156, 216), _rgb(255, 160, 102),
    ),
    "kanagawa-lotus": ThemePalette(
        "kanagawa-lotus", _rgb(77, 105, 155), _rgb(84, 84, 100),
        _rgb(67, 67, 108), _rgb(138, 137, 128), _rgb(98, 76, 131),
        _rgb(111, 137, 78), _rgb(119, 113, 63), _rgb(200, 64, 83),
        _rgb(77, 105, 155), _rgb(204, 109, 0),
    ),
    "rose-pine": ThemePalette(
        "rose-pine", _rgb(196, 167, 231), _rgb(224, 222, 244),
        _rgb(200, 197, 220), _rgb(144, 140, 170), _rgb(196, 167, 231),
        _rgb(49, 116, 143), _rgb(246, 193, 119), _rgb(235, 111, 146),
        _rgb(49, 116, 143), _rgb(234, 154, 151),
    ),
    "rose-pine-dawn": ThemePalette(
        "rose-pine-dawn", _rgb(144, 122, 169), _rgb(70, 66, 97),
        _rgb(121, 117, 147), _rgb(121, 117, 147), _rgb(144, 122, 169),
        _rgb(40, 105, 131), _rgb(234, 157, 52), _rgb(180, 99, 122),
        _rgb(40, 105, 131), _rgb(215, 130, 126),
    ),
    "vesper": ThemePalette(
        "vesper", _rgb(255, 199, 153), _rgb(255, 255, 255),
        _rgb(160, 160, 160), _rgb(126, 126, 126), _rgb(255, 209, 168),
        _rgb(153, 255, 228), _rgb(255, 199, 153), _rgb(255, 128, 128),
        _rgb(176, 176, 176), _rgb(255, 199, 153),
    ),
}


HERDR_THEME_ALIASES: Mapping[str, str] = {
    "catppuccin-mocha": "catppuccin",
    "latte": "catppuccin-latte",
    "light": "catppuccin-latte",
    "tokyonight": "tokyo-night",
    "tokyo-day": "tokyo-night-day",
    "tokyonight-day": "tokyo-night-day",
    "gruvbox-dark": "gruvbox",
    "onedark": "one-dark",
    "onelight": "one-light",
    "solarized-dark": "solarized",
    "lotus": "kanagawa-lotus",
    "rosepine": "rose-pine",
    "rosepine-dawn": "rose-pine-dawn",
    "dawn": "rose-pine-dawn",
}


THEME_CUSTOM_TO_PALETTE: Mapping[str, str] = {
    "accent": "accent",
    "text": "text",
    "subtext0": "muted",
    "overlay1": "subtle",
    "mauve": "branch",
    "green": "positive",
    "yellow": "warning",
    "red": "danger",
    "blue": "info",
    "peach": "peach",
}

NAMED_THEME_COLORS: Mapping[str, int] = {
    "black": 30,
    "red": 31,
    "green": 32,
    "yellow": 33,
    "blue": 34,
    "magenta": 35,
    "purple": 35,
    "cyan": 36,
    "white": 37,
    "gray": 37,
    "grey": 37,
    "darkgray": 90,
    "darkgrey": 90,
    "lightred": 91,
    "lightgreen": 92,
    "lightyellow": 93,
    "lightblue": 94,
    "lightmagenta": 95,
    "lightcyan": 96,
}

_INVALID_THEME_COLOR = object()


def _normalize_theme_name(value: str) -> str:
    return value.strip().lower().replace(" ", "-").replace("_", "-")


def _strip_toml_comment(line: str) -> str:
    quote: Optional[str] = None
    escaped = False
    result: List[str] = []
    for character in line:
        if escaped:
            result.append(character)
            escaped = False
            continue
        if quote == '"' and character == "\\":
            result.append(character)
            escaped = True
            continue
        if character in ("'", '"'):
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            result.append(character)
            continue
        if character == "#" and quote is None:
            break
        result.append(character)
    return "".join(result).strip()


def _parse_theme_scalar(value: str) -> Optional[Union[str, bool]]:
    value = value.strip()
    if value == "true":
        return True
    if value == "false":
        return False
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, str) else None
    return None


def _theme_config_path(env: Mapping[str, str]) -> Path:
    configured = env.get("HERDR_CONFIG_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "herdr" / "config.toml"


def load_theme_settings(
    env: Mapping[str, str],
    *,
    config_path: Optional[Path] = None,
) -> ThemeSettings:
    path = config_path if config_path is not None else _theme_config_path(env)
    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ThemeSettings()

    section = ""
    values: Dict[str, Union[str, bool]] = {}
    custom: Dict[str, str] = {}
    legacy_accent: Optional[str] = None
    for raw_line in contents.splitlines():
        line = _strip_toml_comment(raw_line)
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        raw_key, raw_value = line.split("=", 1)
        key = raw_key.strip()
        parsed = _parse_theme_scalar(raw_value)
        if parsed is None:
            continue
        if section == "theme" and key in ("name", "auto_switch", "dark_name", "light_name"):
            values[key] = parsed
        elif section == "theme.custom" and key in THEME_CUSTOM_TO_PALETTE:
            if isinstance(parsed, str):
                custom[key] = parsed
        elif section == "ui" and key == "accent" and isinstance(parsed, str):
            legacy_accent = parsed

    name = values.get("name")
    auto_switch = values.get("auto_switch")
    dark_name = values.get("dark_name")
    light_name = values.get("light_name")
    return ThemeSettings(
        name=name if isinstance(name, str) and name.strip() else "catppuccin",
        auto_switch=auto_switch if isinstance(auto_switch, bool) else False,
        dark_name=dark_name if isinstance(dark_name, str) and dark_name.strip() else None,
        light_name=light_name if isinstance(light_name, str) and light_name.strip() else None,
        custom=custom,
        legacy_accent=legacy_accent,
    )


def load_worktree_directory(
    env: Mapping[str, str],
    *,
    config_path: Optional[Path] = None,
) -> Optional[Path]:
    path = config_path if config_path is not None else _theme_config_path(env)
    configured: Optional[str] = None
    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        contents = ""
    section = ""
    for raw_line in contents.splitlines():
        line = _strip_toml_comment(raw_line)
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if section != "worktrees" or "=" not in line:
            continue
        raw_key, raw_value = line.split("=", 1)
        if raw_key.strip() != "directory":
            continue
        parsed = _parse_theme_scalar(raw_value)
        if isinstance(parsed, str) and parsed.strip():
            configured = parsed
    candidate = Path(configured or "~/.herdr/worktrees").expanduser()
    if not candidate.is_absolute():
        return None
    return _canonical(candidate)


def _theme_siblings(name: str) -> Tuple[str, str]:
    normalized = HERDR_THEME_ALIASES.get(_normalize_theme_name(name), _normalize_theme_name(name))
    if normalized in ("catppuccin", "catppuccin-latte"):
        return "catppuccin", "catppuccin-latte"
    if normalized in ("tokyo-night", "tokyo-night-day"):
        return "tokyo-night", "tokyo-night-day"
    if normalized in ("gruvbox", "gruvbox-light"):
        return "gruvbox", "gruvbox-light"
    if normalized in ("one-dark", "one-light"):
        return "one-dark", "one-light"
    if normalized in ("solarized", "solarized-light"):
        return "solarized", "solarized-light"
    if normalized in ("kanagawa", "kanagawa-lotus"):
        return "kanagawa", "kanagawa-lotus"
    if normalized in ("rose-pine", "rose-pine-dawn"):
        return "rose-pine", "rose-pine-dawn"
    return normalized, normalized


def _parse_theme_color(value: str) -> Any:
    normalized = value.strip().lower()
    if normalized in ("reset", "default", "none", "transparent"):
        return None
    if normalized in NAMED_THEME_COLORS:
        return NAMED_THEME_COLORS[normalized]
    if normalized.startswith("#"):
        hexadecimal = normalized[1:]
        if len(hexadecimal) == 3 and all(character in "0123456789abcdef" for character in hexadecimal):
            return tuple(int(character * 2, 16) for character in hexadecimal)
        if len(hexadecimal) == 6 and all(character in "0123456789abcdef" for character in hexadecimal):
            return tuple(int(hexadecimal[index:index + 2], 16) for index in (0, 2, 4))
        return _INVALID_THEME_COLOR
    match = re.fullmatch(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", normalized)
    if match:
        components = tuple(int(component) for component in match.groups())
        return components if all(component <= 255 for component in components) else _INVALID_THEME_COLOR
    return _INVALID_THEME_COLOR


def resolve_theme_palette(
    settings: ThemeSettings,
    *,
    appearance: Optional[str] = None,
) -> ThemePalette:
    requested_name = settings.name
    fallback_name = "catppuccin"
    if settings.auto_switch:
        dark_name, light_name = _theme_siblings(settings.name)
        if appearance == "light":
            requested_name = settings.light_name or light_name
            fallback_name = "catppuccin-latte"
        else:
            requested_name = settings.dark_name or dark_name
    normalized = _normalize_theme_name(requested_name)
    normalized = HERDR_THEME_ALIASES.get(normalized, normalized)
    palette = HERDR_THEME_PALETTES.get(normalized, HERDR_THEME_PALETTES[fallback_name])

    replacements: Dict[str, ColorValue] = {}
    for custom_name, value in settings.custom.items():
        parsed = _parse_theme_color(value)
        if parsed is not _INVALID_THEME_COLOR:
            replacements[THEME_CUSTOM_TO_PALETTE[custom_name]] = parsed
    if "accent" not in replacements and settings.legacy_accent:
        parsed_accent = _parse_theme_color(settings.legacy_accent)
        if parsed_accent is not _INVALID_THEME_COLOR:
            replacements["accent"] = parsed_accent
    if replacements:
        palette = dataclasses.replace(palette, **replacements)
    return dataclasses.replace(palette, name=normalized if normalized in HERDR_THEME_PALETTES else fallback_name)


def _parse_terminal_rgb_component(value: str) -> Optional[int]:
    if not value or len(value) > 4 or any(character not in "0123456789abcdefABCDEF" for character in value):
        return None
    raw = int(value, 16)
    maximum = (1 << (len(value) * 4)) - 1
    return (raw * 255 + maximum // 2) // maximum


def _appearance_from_terminal_response(response: str) -> Optional[str]:
    match = re.search(r"\x1b\]11;rgb:([^/]+)/([^/]+)/([^\x07\x1b]+)(?:\x07|\x1b\\)", response)
    if not match:
        match = re.search(r"\x1b\]11;#([0-9a-fA-F]{6})(?:\x07|\x1b\\)", response)
        if not match:
            return None
        hexadecimal = match.group(1)
        red, green, blue = (int(hexadecimal[index:index + 2], 16) for index in (0, 2, 4))
    else:
        components = tuple(_parse_terminal_rgb_component(value) for value in match.groups())
        if any(component is None for component in components):
            return None
        red, green, blue = components  # type: ignore[misc]
    luminance = red * 299 + green * 587 + blue * 114
    return "light" if luminance >= 128_000 else "dark"


def query_terminal_appearance(timeout_seconds: float = 0.15) -> Optional[str]:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    input_fd = sys.stdin.fileno()
    try:
        original = termios.tcgetattr(input_fd)
    except (OSError, termios.error):
        return None
    collected = bytearray()
    deadline = time.monotonic() + timeout_seconds
    try:
        updated = list(original)
        updated[3] &= ~(termios.ICANON | termios.ECHO)
        updated[6][termios.VMIN] = 0
        updated[6][termios.VTIME] = 0
        termios.tcsetattr(input_fd, termios.TCSANOW, updated)
        sys.stdout.write("\033]11;?\033\\")
        sys.stdout.flush()
        while time.monotonic() < deadline:
            ready, _, _ = select.select([input_fd], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(input_fd, 256)
            if not chunk:
                break
            collected.extend(chunk)
            decoded = collected.decode("ascii", errors="ignore")
            appearance = _appearance_from_terminal_response(decoded)
            if appearance:
                return appearance
    except (OSError, termios.error):
        return None
    finally:
        with contextlib.suppress(OSError, termios.error):
            termios.tcsetattr(input_fd, termios.TCSANOW, original)
    return None


def _run_git(
    cwd: Path,
    args: Sequence[str],
    *,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(cwd), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=text,
        )
    except FileNotFoundError as exc:
        raise BootstrapError("git is required but was not found on PATH") from exc
    if check and completed.returncode != 0:
        stderr = completed.stderr.strip() if text else completed.stderr.decode("utf-8", "replace").strip()
        raise BootstrapError(f"git {' '.join(args)} failed: {stderr or 'unknown error'}")
    return completed


def _canonical(path: Path) -> Path:
    return Path(os.path.realpath(os.fspath(path)))


def _absolute_path(path: Path) -> Path:
    """Resolve parent aliases without following a possibly replaced final symlink."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    return _canonical(absolute.parent) / absolute.name


def _git_common_dir(worktree: Path) -> Path:
    result = _run_git(worktree, ["rev-parse", "--git-common-dir"])
    value = result.stdout.strip()
    common = Path(value)
    if not common.is_absolute():
        common = worktree / common
    return _canonical(common)


def _git_root(path: Path) -> Path:
    result = _run_git(path, ["rev-parse", "--show-toplevel"])
    return _canonical(Path(result.stdout.strip()))


def _is_bare(path: Path) -> bool:
    result = _run_git(path, ["rev-parse", "--is-bare-repository"])
    return result.stdout.strip() == "true"


def parse_worktree_records(raw: bytes) -> List[Mapping[str, Any]]:
    """Parse `git worktree list --porcelain -z` without path quoting."""

    records: List[Mapping[str, Any]] = []
    current: Dict[str, Any] = {}
    for field in raw.split(b"\0"):
        if not field:
            if current:
                records.append(current)
                current = {}
            continue
        key, separator, value = field.partition(b" ")
        current[os.fsdecode(key)] = os.fsdecode(value) if separator else True
    if current:
        records.append(current)
    return records


def parse_worktree_porcelain(raw: bytes) -> List[Path]:
    """Extract worktree paths from `git worktree list --porcelain -z`."""

    return [Path(record["worktree"]) for record in parse_worktree_records(raw) if "worktree" in record]


def find_primary_checkout(target: Path, common_git_dir: Path) -> Optional[Path]:
    result = _run_git(target, ["worktree", "list", "--porcelain", "-z"], text=False)
    paths = parse_worktree_porcelain(result.stdout)
    for raw_path in paths:
        candidate = _canonical(raw_path)
        if not candidate.exists():
            continue
        probe = _run_git(candidate, ["rev-parse", "--absolute-git-dir"], check=False)
        if probe.returncode != 0:
            continue
        if _canonical(Path(probe.stdout.strip())) != common_git_dir:
            continue
        try:
            if _git_common_dir(candidate) == common_git_dir and not _is_bare(candidate):
                return _git_root(candidate)
        except BootstrapError:
            continue
    return None


def _decode_json_env(env: Mapping[str, str], name: str) -> Optional[Mapping[str, Any]]:
    raw = env.get(name)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"{name} is not valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise BootstrapError(f"{name} must contain a JSON object")
    return parsed


def _nested_string(data: Mapping[str, Any], keys: Sequence[str]) -> Optional[str]:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current if isinstance(current, str) and current else None


def resolve_target_path(
    explicit_target: Optional[str],
    env: Mapping[str, str],
    *,
    cwd: Optional[Path] = None,
) -> Path:
    """Resolve explicit, event, and action targets using one precedence chain."""

    if explicit_target:
        return _canonical(Path(explicit_target).expanduser())

    event = _decode_json_env(env, "HERDR_PLUGIN_EVENT_JSON")
    if event:
        for keys in (
            ("data", "worktree", "path"),
            ("worktree", "path"),
            ("data", "workspace", "worktree", "path"),
        ):
            value = _nested_string(event, keys)
            if value:
                return _canonical(Path(value).expanduser())

    context = _decode_json_env(env, "HERDR_PLUGIN_CONTEXT_JSON")
    if context:
        for keys in (
            ("workspace_cwd",),
            ("workspace", "cwd"),
            ("workspace", "path"),
            ("worktree", "path"),
            ("cwd",),
            ("focused_pane_cwd",),
            ("pane", "cwd"),
        ):
            value = _nested_string(context, keys)
            if value:
                return _canonical(Path(value).expanduser())

    fallback = cwd if cwd is not None else Path.cwd()
    return _canonical(fallback)


def resolve_repository(target_path: Path) -> RepositoryContext:
    if not target_path.exists():
        raise NothingToDo(f"target checkout is unavailable: {target_path}")
    bare_probe = _run_git(target_path, ["rev-parse", "--is-bare-repository"], check=False)
    if bare_probe.returncode != 0:
        raise NotGitRepository("this workspace is not a Git repository")
    if bare_probe.stdout.strip() == "true":
        raise NothingToDo("bare repositories do not have worktree files to bootstrap")
    target = _git_root(target_path)
    common_git_dir = _git_common_dir(target)
    source = find_primary_checkout(target, common_git_dir)
    if source is None:
        raise NothingToDo("the primary checkout is unavailable; nothing was changed")
    if _git_common_dir(source) != common_git_dir:
        raise BootstrapError("source and target do not share the same common Git directory")
    return RepositoryContext(
        target=target,
        source=source,
        common_git_dir=common_git_dir,
        target_is_primary=target == source,
    )


def validate_relative_path(value: str) -> str:
    if not isinstance(value, str):
        raise BootstrapError("copy-list paths must be strings")
    if not value:
        raise BootstrapError("copy-list paths cannot be empty")
    if "\x00" in value:
        raise BootstrapError("copy-list paths cannot contain NUL bytes")
    if "\n" in value or "\r" in value:
        raise BootstrapError("copy-list paths cannot contain line breaks")
    if os.path.isabs(value) or PurePosixPath(value).is_absolute():
        raise BootstrapError(f"absolute path is not allowed: {value}")
    raw_parts = value.split("/")
    if value == "." or any(part in (".", "..", "") for part in raw_parts):
        raise BootstrapError(f"ambiguous or escaping path is not allowed: {value}")
    return value


def validate_copy_paths(paths: Iterable[str]) -> List[str]:
    validated: List[str] = []
    seen = set()
    for value in paths:
        path = validate_relative_path(value)
        if path in seen:
            raise BootstrapError(f"duplicate copy-list path: {path}")
        seen.add(path)
        validated.append(path)

    component_paths = [(path, tuple(path.split("/"))) for path in validated]
    for index, (left, left_parts) in enumerate(component_paths):
        for right, right_parts in component_paths[index + 1 :]:
            shorter, longer = (left_parts, right_parts)
            if len(shorter) > len(longer):
                shorter, longer = longer, shorter
            if len(shorter) < len(longer) and longer[: len(shorter)] == shorter:
                raise BootstrapError(f"overlapping copy-list paths are not allowed: {left} and {right}")
    return validated


def read_copy_list(source: Path) -> List[str]:
    path = source / COPY_LIST
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BootstrapError(f"cannot read {COPY_LIST}: {exc}") from exc
    values: List[str] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        value = raw.strip()
        if line_number == 1:
            value = value.lstrip("\ufeff")
        if not value or value.startswith("#"):
            continue
        try:
            values.append(validate_relative_path(value))
        except BootstrapError as exc:
            raise BootstrapError(f"{COPY_LIST}:{line_number}: {exc}") from exc
    return validate_copy_paths(values)


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _git_has_tracked_path(source: Path, relative: str) -> bool:
    result = _run_git(source, ["ls-files", "-z", "--", relative], check=False, text=False)
    if result.returncode != 0:
        raise BootstrapError(f"could not classify tracked path: {relative}")
    return bool(result.stdout)


def _git_path_is_ignored(source: Path, relative: str) -> bool:
    result = _run_git(source, ["check-ignore", "-q", "--no-index", "--", relative], check=False)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise BootstrapError(f"could not classify ignored path: {relative}")


def classify_copy_entry(source: Path, target: Path, relative: str) -> CopyEntryStatus:
    _ensure_no_symlink_parents(source, relative)
    _ensure_no_symlink_parents(target, relative)
    source_path = source / relative
    target_path = target / relative
    source_exists = _path_lexists(source_path)
    target_exists = _path_lexists(target_path)
    if _git_has_tracked_path(source, relative):
        eligibility = "tracked"
        detail = "tracked by Git; skipped"
    elif not _git_path_is_ignored(source, relative):
        eligibility = "unignored"
        detail = "not ignored by Git; skipped"
    elif not source_exists:
        eligibility = "missing"
        detail = "source does not exist; skipped"
    else:
        eligibility = "ignored"
        detail = "ready to copy"
    return CopyEntryStatus(relative, source_exists, target_exists, eligibility, detail)


def classify_copy_entries(source: Path, target: Path, paths: Sequence[str]) -> List[CopyEntryStatus]:
    return [classify_copy_entry(source, target, path) for path in paths]


def _ensure_no_symlink_parents(root: Path, relative: str, *, create: bool = False) -> None:
    current = root
    parts = relative.split("/")[:-1]
    for part in parts:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if not create:
                continue
            try:
                current.mkdir()
            except FileExistsError:
                metadata = current.lstat()
            else:
                metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise BootstrapError(f"refusing to traverse symlink parent: {current.relative_to(root)}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise BootstrapError(f"path parent is not a directory: {current.relative_to(root)}")


def _copy_object(source: Path, destination: Path) -> None:
    metadata = source.lstat()
    mode = metadata.st_mode
    if stat.S_ISLNK(mode):
        os.symlink(os.readlink(os.fspath(source)), os.fspath(destination))
    elif stat.S_ISREG(mode):
        shutil.copy2(source, destination, follow_symlinks=False)
    elif stat.S_ISDIR(mode):
        shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
    else:
        raise BootstrapError(f"unsupported file type in copy list: {source.name}")


def _remove_object(path: Path) -> None:
    if not _path_lexists(path):
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def cleanup_stale_transactions(target: Path) -> int:
    cleaned = 0
    try:
        candidates = list(target.iterdir())
    except OSError as exc:
        raise BootstrapError(f"cannot inspect target for stale transactions: {exc}") from exc
    for candidate in candidates:
        if not candidate.name.startswith(TXN_PREFIX):
            continue
        try:
            _remove_object(candidate)
        except OSError as exc:
            raise BootstrapError(f"cannot remove stale transaction {candidate.name}: {exc}") from exc
        cleaned += 1
    return cleaned


def sync_paths(context: RepositoryContext, statuses: Sequence[CopyEntryStatus]) -> int:
    copyable = [status for status in statuses if status.copyable]
    if not copyable:
        return 0

    transaction = context.target / f"{TXN_PREFIX}{uuid.uuid4().hex}"
    stage_dir = transaction / "stage"
    backup_dir = transaction / "backup"
    discarded_dir = transaction / "discarded"
    committed: List[Tuple[CopyEntryStatus, Optional[Path]]] = []
    try:
        stage_dir.mkdir(parents=True)
        backup_dir.mkdir()
        discarded_dir.mkdir()

        for index, entry in enumerate(copyable):
            _ensure_no_symlink_parents(context.source, entry.path)
            staged = stage_dir / str(index)
            _copy_object(context.source / entry.path, staged)

        for index, entry in enumerate(copyable):
            _ensure_no_symlink_parents(context.target, entry.path, create=True)
            destination = context.target / entry.path
            staged = stage_dir / str(index)
            backup: Optional[Path] = None
            if _path_lexists(destination):
                backup = backup_dir / str(index)
                os.rename(os.fspath(destination), os.fspath(backup))
            try:
                os.rename(os.fspath(staged), os.fspath(destination))
            except BaseException:
                if backup is not None and not _path_lexists(destination):
                    os.rename(os.fspath(backup), os.fspath(destination))
                raise
            committed.append((entry, backup))
        return len(committed)
    except BaseException as original:
        rollback_errors: List[str] = []
        for rollback_index, (entry, backup) in enumerate(reversed(committed)):
            destination = context.target / entry.path
            try:
                if _path_lexists(destination):
                    os.rename(
                        os.fspath(destination),
                        os.fspath(discarded_dir / str(rollback_index)),
                    )
                if backup is not None and _path_lexists(backup):
                    os.rename(os.fspath(backup), os.fspath(destination))
            except OSError as exc:
                rollback_errors.append(f"{entry.path}: {exc}")
        if isinstance(original, KeyboardInterrupt):
            raise
        message = str(original)
        if rollback_errors:
            message += "; rollback errors: " + "; ".join(rollback_errors)
        if isinstance(original, BootstrapError):
            raise BootstrapError(message, original.exit_code) from original
        raise BootstrapError(f"copy transaction failed: {message}") from original
    finally:
        with contextlib.suppress(OSError):
            _remove_object(transaction)


def _parse_lifecycle_command_list(raw_commands: Any, label: str) -> List[SetupCommand]:
    if not isinstance(raw_commands, list):
        raise BootstrapError(f"{label} commands must be an array")
    commands: List[SetupCommand] = []
    allowed = {"name", "argv", "timeout_seconds"}
    for index, raw in enumerate(raw_commands):
        command_label = f"{label} command {index + 1}"
        if not isinstance(raw, dict) or set(raw) != allowed:
            raise BootstrapError(
                f"{command_label} must contain only name, argv, and timeout_seconds"
            )
        name = raw["name"]
        argv = raw["argv"]
        timeout = raw["timeout_seconds"]
        if not isinstance(name, str) or not name.strip():
            raise BootstrapError(f"{command_label} name must be a non-empty string")
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            raise BootstrapError(f"{command_label} argv must be a non-empty array of strings")
        if not argv[0]:
            raise BootstrapError(f"{command_label} executable cannot be empty")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise BootstrapError(f"{command_label} timeout_seconds must be a positive integer")
        commands.append(SetupCommand(name=name.strip(), argv=tuple(argv), timeout_seconds=timeout))
    return commands


def load_lifecycle_commands(source: Path) -> Mapping[str, List[SetupCommand]]:
    path = source / SETUP_FILE
    if not path.exists():
        return {phase: [] for phase in LIFECYCLE_PHASES}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"{SETUP_FILE} is invalid JSON at line {exc.lineno}: {exc.msg}") from exc
    except (OSError, UnicodeError) as exc:
        raise BootstrapError(f"cannot read {SETUP_FILE}: {exc}") from exc
    if not isinstance(document, dict):
        raise BootstrapError(f"{SETUP_FILE} must contain a JSON object")
    version = document.get("version")
    if isinstance(version, bool):
        raise BootstrapError(f"{SETUP_FILE} version must be 1 or 2")
    if version == 1:
        if set(document) != {"version", "commands"}:
            raise BootstrapError(f"{SETUP_FILE} must contain only 'version' and 'commands'")
        result = {phase: [] for phase in LIFECYCLE_PHASES}
        result["on_created"] = _parse_lifecycle_command_list(
            document["commands"], os.fspath(SETUP_FILE)
        )
        return result
    if version != 2:
        raise BootstrapError(f"{SETUP_FILE} version must be 1 or 2")
    if set(document) != {"version", "hooks"} or not isinstance(document.get("hooks"), dict):
        raise BootstrapError(f"{SETUP_FILE} version 2 must contain only 'version' and 'hooks'")
    hooks = document["hooks"]
    unknown = set(hooks) - set(LIFECYCLE_PHASES)
    if unknown:
        raise BootstrapError(f"{SETUP_FILE} has unknown lifecycle hook: {sorted(unknown)[0]}")
    return {
        phase: _parse_lifecycle_command_list(
            hooks.get(phase, []), f"{SETUP_FILE} {phase}"
        )
        for phase in LIFECYCLE_PHASES
    }


def load_setup_commands(source: Path) -> List[SetupCommand]:
    return list(load_lifecycle_commands(source)["on_created"])


def _terminate_process_group(process: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run_lifecycle_commands(
    target: Path,
    commands: Sequence[SetupCommand],
    *,
    phase: str,
    environment: Optional[Mapping[str, str]] = None,
) -> int:
    if phase not in LIFECYCLE_PHASES:
        raise BootstrapError(f"unknown lifecycle phase: {phase}")
    phase_label = "setup" if phase == "on_created" else phase.replace("_", "-")
    child_env = os.environ.copy()
    if environment:
        child_env.update({key: value for key, value in environment.items() if isinstance(value, str)})
    completed_count = 0
    for index, command in enumerate(commands, 1):
        print(f"{phase_label} {index}/{len(commands)}: {command.name}", flush=True)
        try:
            process = subprocess.Popen(
                list(command.argv),
                cwd=os.fspath(target),
                start_new_session=True,
                env=child_env,
            )
        except FileNotFoundError as exc:
            raise SetupCommandError(
                f"{phase_label} command {index} ({command.name}) executable not found: {command.argv[0]}",
                command_index=index,
                command_name=command.name,
                failure_kind="executable_not_found",
            ) from exc
        except OSError as exc:
            raise SetupCommandError(
                f"{phase_label} command {index} ({command.name}) could not start: {exc}",
                command_index=index,
                command_name=command.name,
                failure_kind="start_failed",
            ) from exc
        try:
            return_code = process.wait(timeout=command.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process)
            raise SetupCommandError(
                f"{phase_label} command {index} ({command.name}) timed out after {command.timeout_seconds}s",
                command_index=index,
                command_name=command.name,
                failure_kind="timeout",
            ) from exc
        except KeyboardInterrupt:
            _terminate_process_group(process)
            raise
        if return_code != 0:
            raise SetupCommandError(
                f"{phase_label} command {index} ({command.name}) failed with exit code {return_code}",
                command_index=index,
                command_name=command.name,
                failure_kind="nonzero_exit",
                command_exit_code=return_code,
            )
        completed_count += 1
    return completed_count


def run_setup_commands(target: Path, commands: Sequence[SetupCommand]) -> int:
    return run_lifecycle_commands(target, commands, phase="on_created")


def default_state_dir(env: Mapping[str, str]) -> Path:
    if env.get("HERDR_PLUGIN_STATE_DIR"):
        return Path(env["HERDR_PLUGIN_STATE_DIR"]).expanduser()
    if env.get("XDG_STATE_HOME"):
        return Path(env["XDG_STATE_HOME"]).expanduser() / "herdr" / "plugins" / PLUGIN_ID
    return Path.home() / ".local" / "state" / "herdr" / "plugins" / PLUGIN_ID


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=os.fspath(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _read_json_file(path: Path) -> Optional[Mapping[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _path_state_key(path: Path) -> str:
    encoded = os.fspath(_absolute_path(path)).encode("utf-8", "surrogateescape")
    return hashlib.sha256(encoded).hexdigest()[:24]


def worktree_map_path(state_dir: Path, target: Path) -> Path:
    return state_dir / WORKTREE_MAP_DIR / f"{_path_state_key(target)}.json"


def _workspace_id_from_env(env: Mapping[str, str]) -> Optional[str]:
    for name in ("HERDR_PLUGIN_EVENT_JSON", "HERDR_PLUGIN_CONTEXT_JSON"):
        document = _decode_json_env(env, name)
        if not isinstance(document, dict):
            continue
        for keys in (
            ("data", "workspace_id"),
            ("data", "workspace", "workspace_id"),
            ("workspace_id",),
            ("workspace", "workspace_id"),
            ("workspace", "id"),
        ):
            value = _nested_string(document, keys)
            if value:
                return value
    return None


def _refs_pointing_at(repo_root: Path, oid: str, branch: Optional[str]) -> List[str]:
    result = _run_git(
        repo_root,
        [
            "for-each-ref",
            "--format=%(refname:short)",
            f"--points-at={oid}",
            "refs/heads",
            "refs/remotes",
        ],
        check=False,
    )
    if result.returncode != 0:
        return []
    return sorted(
        {
            value.strip()
            for value in result.stdout.splitlines()
            if value.strip() and value.strip() != branch
        }
    )


def record_worktree_mapping(
    context: RepositoryContext,
    state_dir: Path,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> None:
    if context.target_is_primary:
        return
    branch_result = _run_git(
        context.target,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        check=False,
    )
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    oid_result = _run_git(context.target, ["rev-parse", "--verify", "HEAD"], check=False)
    head_oid = oid_result.stdout.strip() if oid_result.returncode == 0 else None
    existing = load_worktree_mapping(state_dir, context.target)
    preserve_creation = (
        isinstance(existing, dict)
        and existing.get("status", "active") == "active"
        and existing.get("common_git_dir") == os.fspath(context.common_git_dir)
    )
    workspace_id = _workspace_id_from_env(env or {})
    payload: Dict[str, Any] = {
        "version": 2,
        "target": os.fspath(context.target),
        "repo_root": os.fspath(context.source),
        "common_git_dir": os.fspath(context.common_git_dir),
        "branch": branch or None,
        "created_head_oid": (
            existing.get("created_head_oid") if preserve_creation else head_oid
        ),
        "base_candidates": (
            existing.get("base_candidates", [])
            if preserve_creation
            else (_refs_pointing_at(context.source, head_oid, branch) if head_oid else [])
        ),
        "workspace_id": workspace_id or (existing.get("workspace_id") if preserve_creation else None),
        "recorded_at": (
            existing.get("recorded_at")
            if preserve_creation
            else dt.datetime.now(dt.timezone.utc).isoformat()
        ),
        "status": "active",
        "provenance_verified": True,
    }
    _atomic_write_json(worktree_map_path(state_dir, context.target), payload)


def load_worktree_mapping(state_dir: Path, target: Path) -> Optional[Mapping[str, Any]]:
    return _read_json_file(worktree_map_path(state_dir, target))


def list_worktree_mappings(state_dir: Path) -> List[Mapping[str, Any]]:
    directory = state_dir / WORKTREE_MAP_DIR
    if not directory.exists():
        return []
    records: List[Mapping[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        document = _read_json_file(path)
        if document is not None:
            records.append(dict(document))
    return records


def mark_worktree_removed(
    state_dir: Path,
    worktree_path: Path,
    repo_root: Path,
    *,
    common_git_dir: Optional[Path],
    branch: Optional[str],
    forced: bool,
) -> Mapping[str, Any]:
    existing = load_worktree_mapping(state_dir, worktree_path)
    common_value = os.fspath(common_git_dir) if common_git_dir is not None else None
    verified = bool(
        isinstance(existing, dict)
        and isinstance(existing.get("common_git_dir"), str)
        and common_value is not None
        and _canonical(Path(str(existing["common_git_dir"]))) == _canonical(common_git_dir)
    )
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    payload: Dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    payload.update(
        {
            "version": 2,
            "target": os.fspath(_absolute_path(worktree_path)),
            "repo_root": os.fspath(_canonical(repo_root)),
            "common_git_dir": common_value or payload.get("common_git_dir"),
            "branch": branch,
            "status": "removed",
            "removed_at": now,
            "forced_removal": forced,
            "provenance_verified": verified,
        }
    )
    if "recorded_at" not in payload:
        payload["recorded_at"] = now
    _atomic_write_json(worktree_map_path(state_dir, worktree_path), payload)
    return payload


def inspect_leftover_path(path: Path) -> LeftoverInspection:
    path = _absolute_path(path)
    try:
        root_stat = path.lstat()
    except FileNotFoundError:
        return LeftoverInspection(path, False, False, False, 0, 0, (), False, None, None)
    except OSError:
        return LeftoverInspection(path, True, False, False, 0, 0, (), True, None, None)
    is_symlink = stat.S_ISLNK(root_stat.st_mode)
    is_directory = stat.S_ISDIR(root_stat.st_mode)
    if not is_directory or is_symlink:
        return LeftoverInspection(
            path,
            True,
            is_directory,
            is_symlink,
            1,
            root_stat.st_size if stat.S_ISREG(root_stat.st_mode) else 0,
            (path.name,),
            False,
            root_stat.st_dev,
            root_stat.st_ino,
        )

    entry_count = 0
    total_bytes = 0
    sample: List[str] = []
    truncated = False
    stack: List[Path] = [path]
    while stack and entry_count < LEFTOVER_ENTRY_LIMIT:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            truncated = True
            continue
        for entry_index, entry in enumerate(entries):
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                truncated = True
                continue
            entry_path = Path(entry.path)
            if stat.S_ISDIR(entry_stat.st_mode) and not stat.S_ISLNK(entry_stat.st_mode):
                stack.append(entry_path)
                continue
            entry_count += 1
            if stat.S_ISREG(entry_stat.st_mode):
                total_bytes += entry_stat.st_size
            if len(sample) < LEFTOVER_SAMPLE_LIMIT:
                with contextlib.suppress(ValueError):
                    sample.append(entry_path.relative_to(path).as_posix())
            if entry_count >= LEFTOVER_ENTRY_LIMIT:
                truncated = bool(stack) or entry_index < len(entries) - 1
                break
    return LeftoverInspection(
        path,
        True,
        True,
        False,
        entry_count,
        total_bytes,
        tuple(sample),
        truncated,
        root_stat.st_dev,
        root_stat.st_ino,
    )


def collect_worktree_inventory(
    repo_root: Path,
    state_dir: Path,
    *,
    worktree_root: Optional[Path] = None,
) -> List[WorktreeInventoryItem]:
    repo_root = _validate_cleanup_repository(_canonical(repo_root))
    common_git_dir = _git_common_dir(repo_root)
    listed = _run_git(repo_root, ["worktree", "list", "--porcelain", "-z"], text=False)
    live_records: Dict[Path, Mapping[str, Any]] = {}
    for record in parse_worktree_records(listed.stdout):
        raw_path = record.get("worktree")
        if isinstance(raw_path, str):
            live_records[_absolute_path(Path(raw_path))] = record

    mappings: Dict[Path, Mapping[str, Any]] = {}
    for record in list_worktree_mappings(state_dir):
        raw_target = record.get("target")
        raw_common = record.get("common_git_dir")
        raw_repo = record.get("repo_root")
        if not isinstance(raw_target, str):
            continue
        belongs = False
        if isinstance(raw_common, str):
            belongs = _canonical(Path(raw_common)) == common_git_dir
        elif isinstance(raw_repo, str):
            belongs = _canonical(Path(raw_repo)) == repo_root
        if belongs:
            mappings[_absolute_path(Path(raw_target))] = record

    discovered_paths: set[Path] = set()
    if worktree_root is not None:
        bucket = _canonical(worktree_root) / repo_root.name
        try:
            discovered_paths = {
                _absolute_path(Path(entry.path))
                for entry in os.scandir(bucket)
            }
        except OSError:
            discovered_paths = set()

    items: List[WorktreeInventoryItem] = []
    for path in sorted(
        set(live_records) | set(mappings) | discovered_paths,
        key=lambda value: os.fspath(value).casefold(),
    ):
        live = live_records.get(path)
        mapping = mappings.get(path, {})
        registered = live is not None
        raw_branch = live.get("branch") if live is not None else mapping.get("branch")
        branch: Optional[str] = None
        if isinstance(raw_branch, str) and raw_branch:
            branch = raw_branch.removeprefix("refs/heads/")
        raw_head = live.get("HEAD") if live is not None else None
        head_oid = raw_head if isinstance(raw_head, str) and raw_head else None
        leftover = inspect_leftover_path(path)
        branch_exists = _ref_exists(repo_root, f"refs/heads/{branch}") if branch else None
        if path == repo_root:
            status_value = "primary"
        elif registered and leftover.exists and leftover.is_directory and not leftover.is_symlink:
            status_value = "active"
        elif registered:
            status_value = "missing"
        elif leftover.exists:
            status_value = "residue"
        elif branch_exists:
            status_value = "branch-only"
        else:
            status_value = "removed"
        raw_created_oid = mapping.get("created_head_oid")
        raw_candidates = mapping.get("base_candidates")
        base_candidates = tuple(
            candidate for candidate in raw_candidates if isinstance(candidate, str)
        ) if isinstance(raw_candidates, list) else ()
        workspace_id = mapping.get("workspace_id")
        removed_at = mapping.get("removed_at")
        items.append(
            WorktreeInventoryItem(
                repo_root=repo_root,
                worktree_path=path,
                branch=branch,
                head_oid=head_oid,
                created_head_oid=raw_created_oid if isinstance(raw_created_oid, str) else None,
                base_candidates=base_candidates,
                registered=registered,
                path_exists=leftover.exists,
                branch_exists=branch_exists,
                status=status_value,
                provenance_verified=registered or bool(mapping.get("provenance_verified")),
                workspace_id=workspace_id if isinstance(workspace_id, str) else None,
                forced_removal=bool(mapping.get("forced_removal")),
                removed_at=removed_at if isinstance(removed_at, str) else None,
                on_created_status=(
                    mapping.get("on_created_status")
                    if isinstance(mapping.get("on_created_status"), str)
                    else None
                ),
                before_remove_status=(
                    mapping.get("before_remove_status")
                    if isinstance(mapping.get("before_remove_status"), str)
                    else None
                ),
                after_remove_status=(
                    mapping.get("after_remove_status")
                    if isinstance(mapping.get("after_remove_status"), str)
                    else None
                ),
                leftover=leftover,
            )
        )
    return items


def _format_byte_count(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def inventory_lines(
    repo_root: Path,
    state_dir: Path,
    *,
    env: Optional[Mapping[str, str]] = None,
    items: Optional[Sequence[WorktreeInventoryItem]] = None,
) -> List[str]:
    effective_env = {} if env is None else env
    inventory = list(items) if items is not None else collect_worktree_inventory(
        repo_root, state_dir, worktree_root=load_worktree_directory(effective_env)
    )
    lifecycle = load_lifecycle_commands(_canonical(repo_root))
    lines = [
        f"Repository: {_canonical(repo_root)}",
        f"Worktrees: {len(inventory)}",
        "Lifecycle: " + ", ".join(
            f"{phase}={len(lifecycle[phase])}" for phase in LIFECYCLE_PHASES
        ),
    ]
    for index, item in enumerate(inventory, 1):
        branch = item.branch or "(detached/unknown)"
        flags = [
            f"git={'yes' if item.registered else 'no'}",
            f"branch={('yes' if item.branch_exists else 'no') if item.branch_exists is not None else 'n/a'}",
            f"path={'yes' if item.path_exists else 'no'}",
            f"provenance={'verified' if item.provenance_verified else 'unverified'}",
        ]
        lines.append(f"{index}. [{item.status}] {branch}  {'  '.join(flags)}")
        lines.append(f"   path: {item.worktree_path}")
        created_oid = item.created_head_oid or item.head_oid
        if created_oid:
            detail = created_oid[:12]
            if item.base_candidates:
                detail += "  base candidates: " + ", ".join(item.base_candidates[:3])
            lines.append(f"   created at: {detail}")
        if item.status == "residue":
            suffix = "+" if item.leftover.truncated else ""
            lines.append(
                f"   residue: {item.leftover.entry_count}{suffix} files, "
                f"{_format_byte_count(item.leftover.total_bytes)}"
            )
            for sample in item.leftover.sample_paths[:3]:
                lines.append(f"     - {sample}")
        lifecycle_status = ", ".join(
            f"{label}={value}"
            for label, value in (
                ("created", item.on_created_status),
                ("before-remove", item.before_remove_status),
                ("after-remove", item.after_remove_status),
            )
            if value
        )
        if lifecycle_status:
            lines.append(f"   lifecycle: {lifecycle_status}")
    return lines


def remove_leftover_worktree_files(
    repo_root: Path,
    worktree_path: Path,
    *,
    worktree_root: Optional[Path],
    provenance_verified: bool,
    expected_device: Optional[int],
    expected_inode: Optional[int],
) -> None:
    repo_root = _validate_cleanup_repository(_canonical(repo_root))
    worktree_path = _absolute_path(worktree_path)
    if worktree_root is None:
        raise BootstrapError("refusing cleanup without an absolute configured worktree root")
    configured_root = _canonical(worktree_root)
    try:
        relative = worktree_path.relative_to(configured_root)
    except ValueError as exc:
        raise BootstrapError("refusing to remove a path outside the configured worktree root") from exc
    if not relative.parts:
        raise BootstrapError("refusing to remove the configured worktree root itself")
    if worktree_path == repo_root:
        raise BootstrapError("refusing to remove the primary checkout")
    if not provenance_verified:
        raise BootstrapError("refusing to remove an unverified leftover directory")
    listed = _run_git(repo_root, ["worktree", "list", "--porcelain", "-z"], text=False)
    if worktree_path in {_absolute_path(path) for path in parse_worktree_porcelain(listed.stdout)}:
        raise BootstrapError("refusing to remove a path still registered as a Git worktree")
    inspection = inspect_leftover_path(worktree_path)
    if not inspection.exists:
        return
    if not inspection.is_directory or inspection.is_symlink:
        raise BootstrapError("refusing to remove a leftover path that is not a real directory")
    if _canonical(worktree_path.parent) != worktree_path.parent:
        raise BootstrapError("refusing to remove a leftover path through a changed parent link")
    if (
        expected_device is not None
        and expected_inode is not None
        and (inspection.device, inspection.inode) != (expected_device, expected_inode)
    ):
        raise BootstrapError("leftover directory changed after it was displayed; review it again")
    repository_probe = _run_git(
        worktree_path,
        ["rev-parse", "--show-toplevel"],
        check=False,
    )
    if repository_probe.returncode == 0:
        raise BootstrapError("refusing to remove a path that Git still recognizes as a repository")
    try:
        shutil.rmtree(worktree_path)
    except OSError as exc:
        raise BootstrapError(f"could not remove leftover worktree files: {exc}") from exc
    if os.path.lexists(worktree_path):
        raise BootstrapError(
            "leftover path was recreated during cleanup; stop processes writing to it and retry"
        )


def _pending_cleanup_dir(state_dir: Path) -> Path:
    return state_dir / BRANCH_CLEANUP_DIR / "pending"


def pending_cleanup_id(repo_root: Path, worktree_path: Path, branch: str) -> str:
    raw = f"{_canonical(repo_root)}\0{_absolute_path(worktree_path)}\0{branch}".encode(
        "utf-8", "surrogateescape"
    )
    return hashlib.sha256(raw).hexdigest()[:24]


def pending_cleanup_path(state_dir: Path, cleanup_id: str) -> Path:
    if not cleanup_id or any(character not in "0123456789abcdef" for character in cleanup_id):
        raise BootstrapError("invalid branch cleanup id")
    return _pending_cleanup_dir(state_dir) / f"{cleanup_id}.json"


def list_pending_cleanups(state_dir: Path) -> List[Mapping[str, Any]]:
    directory = _pending_cleanup_dir(state_dir)
    if not directory.exists():
        return []
    records: List[Mapping[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        document = _read_json_file(path)
        if document is None:
            continue
        record = dict(document)
        record["_path"] = os.fspath(path)
        records.append(record)
    return sorted(records, key=lambda item: str(item.get("created_at", "")))


def _normalize_local_branch(branch: str) -> str:
    value = branch.strip()
    prefix = "refs/heads/"
    if value.startswith(prefix):
        value = value[len(prefix) :]
    if not value or "\x00" in value:
        raise BootstrapError("removed worktree did not provide a valid local branch")
    return value


def _removed_event_details(env: Mapping[str, str]) -> Tuple[Path, Optional[Path], str, bool]:
    envelope = _decode_json_env(env, "HERDR_PLUGIN_EVENT_JSON")
    if envelope is None:
        raise BootstrapError("HERDR_PLUGIN_EVENT_JSON is required for branch cleanup")
    event_name = env.get("HERDR_PLUGIN_EVENT") or envelope.get("event")
    if event_name != "worktree.removed":
        raise NothingToDo("event is not worktree.removed; nothing was queued")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise BootstrapError("worktree.removed event data is missing")
    worktree = data.get("worktree")
    if not isinstance(worktree, dict):
        raise BootstrapError("worktree.removed event has no worktree record")
    raw_path = worktree.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise BootstrapError("worktree.removed event has no worktree path")
    raw_branch = worktree.get("branch")
    if raw_branch is None or worktree.get("is_detached") is True:
        raise NothingToDo("removed worktree was detached; no local branch cleanup is needed")
    if not isinstance(raw_branch, str):
        raise BootstrapError("worktree.removed branch must be a string")
    branch = _normalize_local_branch(raw_branch)
    repo_root: Optional[Path] = None
    workspace = data.get("workspace")
    if isinstance(workspace, dict):
        provenance = workspace.get("worktree")
        if isinstance(provenance, dict) and isinstance(provenance.get("repo_root"), str):
            repo_root = _canonical(Path(provenance["repo_root"]))
    return _absolute_path(Path(raw_path)), repo_root, branch, bool(data.get("forced", False))


def _validate_cleanup_repository(repo_root: Path, expected_common: Optional[str] = None) -> Path:
    if not repo_root.exists():
        raise BootstrapError(f"branch cleanup repository is unavailable: {repo_root}")
    if _is_bare(repo_root):
        raise BootstrapError("branch cleanup requires a non-bare primary checkout")
    resolved = _git_root(repo_root)
    common = _git_common_dir(resolved)
    if expected_common and common != _canonical(Path(expected_common)):
        raise BootstrapError("cached worktree and cleanup repository no longer share a Git directory")
    return resolved


def queue_branch_cleanup(env: Mapping[str, str], state_dir: Path) -> Mapping[str, Any]:
    try:
        worktree_path, event_repo_root, branch, forced = _removed_event_details(env)
    except NothingToDo:
        envelope = _decode_json_env(env, "HERDR_PLUGIN_EVENT_JSON")
        data = envelope.get("data") if isinstance(envelope, dict) else None
        worktree = data.get("worktree") if isinstance(data, dict) else None
        raw_path = worktree.get("path") if isinstance(worktree, dict) else None
        if isinstance(raw_path, str) and raw_path:
            removed_path = _absolute_path(Path(raw_path))
            mapping = load_worktree_mapping(state_dir, removed_path)
            raw_repo = mapping.get("repo_root") if isinstance(mapping, dict) else None
            workspace = data.get("workspace") if isinstance(data, dict) else None
            provenance = workspace.get("worktree") if isinstance(workspace, dict) else None
            if isinstance(provenance, dict) and isinstance(provenance.get("repo_root"), str):
                raw_repo = provenance["repo_root"]
            if isinstance(raw_repo, str):
                with contextlib.suppress(BootstrapError):
                    removed_repo = _validate_cleanup_repository(_canonical(Path(raw_repo)))
                    mark_worktree_removed(
                        state_dir,
                        removed_path,
                        removed_repo,
                        common_git_dir=_git_common_dir(removed_repo),
                        branch=None,
                        forced=bool(data.get("forced", False)) if isinstance(data, dict) else False,
                    )
        raise
    mapping = load_worktree_mapping(state_dir, worktree_path)
    cached_repo = mapping.get("repo_root") if isinstance(mapping, dict) else None
    repo_candidate = event_repo_root
    if repo_candidate is None and isinstance(cached_repo, str):
        repo_candidate = Path(cached_repo)
    if repo_candidate is None:
        raise BootstrapError("cannot identify the primary checkout for removed worktree")
    expected_common = mapping.get("common_git_dir") if isinstance(mapping, dict) else None
    repo_root = _validate_cleanup_repository(
        _canonical(repo_candidate),
        expected_common if isinstance(expected_common, str) else None,
    )
    branch_check = _run_git(repo_root, ["check-ref-format", "--branch", branch], check=False)
    if branch_check.returncode != 0:
        raise BootstrapError("removed worktree reported an invalid branch name")
    cleanup_id = pending_cleanup_id(repo_root, worktree_path, branch)
    removed_record = mark_worktree_removed(
        state_dir,
        worktree_path,
        repo_root,
        common_git_dir=_git_common_dir(repo_root),
        branch=branch,
        forced=forced,
    )
    leftover = inspect_leftover_path(worktree_path)
    payload: Dict[str, Any] = {
        "version": 2,
        "id": cleanup_id,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "repo_root": os.fspath(repo_root),
        "worktree_path": os.fspath(worktree_path),
        "branch": branch,
        "forced_worktree_removal": forced,
        "status": "pending",
        "common_git_dir": removed_record.get("common_git_dir"),
        "provenance_verified": bool(removed_record.get("provenance_verified")),
        "leftover_detected_at_queue": leftover.exists,
    }
    _atomic_write_json(pending_cleanup_path(state_dir, cleanup_id), payload)
    return payload


def _ref_exists(repo_root: Path, ref: str) -> bool:
    result = _run_git(repo_root, ["show-ref", "--verify", "--quiet", ref], check=False)
    if result.returncode not in (0, 1):
        raise BootstrapError(f"could not inspect Git ref: {ref}")
    return result.returncode == 0


def _default_branch_ref(repo_root: Path) -> Optional[str]:
    symbolic = _run_git(
        repo_root,
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        check=False,
    )
    if symbolic.returncode == 0 and symbolic.stdout.strip():
        return symbolic.stdout.strip()
    for candidate in ("main", "master", "trunk", "develop", "development"):
        if _ref_exists(repo_root, f"refs/heads/{candidate}"):
            return candidate
    return None


def inspect_branch(repo_root: Path, branch: str) -> BranchInspection:
    repo_root = _validate_cleanup_repository(_canonical(repo_root))
    branch = _normalize_local_branch(branch)
    if _run_git(repo_root, ["check-ref-format", "--branch", branch], check=False).returncode != 0:
        raise BootstrapError("invalid local branch name")
    exists = _ref_exists(repo_root, f"refs/heads/{branch}")
    default_ref = _default_branch_ref(repo_root)
    default_name = default_ref.split("/", 1)[-1] if default_ref else None
    protected = branch in PROTECTED_BRANCHES or branch == default_name
    protection_reason = "protected/default branch" if protected else None

    used_by: List[str] = []
    records_result = _run_git(repo_root, ["worktree", "list", "--porcelain", "-z"], text=False)
    for record in parse_worktree_records(records_result.stdout):
        if record.get("branch") == f"refs/heads/{branch}" and isinstance(record.get("worktree"), str):
            used_by.append(str(record["worktree"]))

    upstream: Optional[str] = None
    ahead: Optional[int] = None
    behind: Optional[int] = None
    last_commit: Optional[str] = None
    head_oid: Optional[str] = None
    merged: Optional[bool] = None
    if exists:
        oid_result = _run_git(
            repo_root,
            ["rev-parse", "--verify", f"refs/heads/{branch}"],
            check=False,
        )
        if oid_result.returncode == 0 and oid_result.stdout.strip():
            head_oid = oid_result.stdout.strip()
        upstream_result = _run_git(
            repo_root,
            ["for-each-ref", "--format=%(upstream:short)", f"refs/heads/{branch}"],
            check=False,
        )
        if upstream_result.returncode == 0 and upstream_result.stdout.strip():
            upstream = upstream_result.stdout.strip()
            counts = _run_git(
                repo_root,
                ["rev-list", "--left-right", "--count", f"{branch}...{upstream}"],
                check=False,
            )
            if counts.returncode == 0:
                parts = counts.stdout.split()
                if len(parts) == 2 and all(part.isdigit() for part in parts):
                    ahead, behind = int(parts[0]), int(parts[1])
        if default_ref and _ref_exists(
            repo_root,
            default_ref if default_ref.startswith("refs/") else (
                f"refs/remotes/{default_ref}" if "/" in default_ref else f"refs/heads/{default_ref}"
            ),
        ):
            merged_result = _run_git(
                repo_root,
                ["merge-base", "--is-ancestor", branch, default_ref],
                check=False,
            )
            if merged_result.returncode in (0, 1):
                merged = merged_result.returncode == 0
        commit_result = _run_git(
            repo_root,
            ["log", "-1", "--format=%h %s", f"refs/heads/{branch}"],
            check=False,
        )
        if commit_result.returncode == 0 and commit_result.stdout.strip():
            last_commit = commit_result.stdout.strip()

    return BranchInspection(
        repo_root=repo_root,
        branch=branch,
        exists=exists,
        protected=protected,
        protection_reason=protection_reason,
        used_by_worktrees=tuple(used_by),
        default_ref=default_ref,
        merged_into_default=merged,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        last_commit=last_commit,
        head_oid=head_oid,
    )


def delete_local_branch(
    repo_root: Path,
    branch: str,
    *,
    force: bool = False,
    expected_oid: Optional[str] = None,
) -> None:
    inspection = inspect_branch(repo_root, branch)
    if not inspection.exists:
        return
    if inspection.protected:
        raise BootstrapError(f"refusing to delete protected branch: {branch}")
    if inspection.used_by_worktrees:
        raise BootstrapError("refusing to delete a branch used by another worktree")
    if expected_oid is not None and inspection.head_oid != expected_oid:
        raise BootstrapError("branch changed after it was displayed; review it again")
    flag = "-D" if force else "-d"
    result = _run_git(inspection.repo_root, ["branch", flag, "--", inspection.branch], check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or "Git refused the branch deletion"
        raise BootstrapError(detail)


def _record_cleanup_result(
    state_dir: Path,
    record: Mapping[str, Any],
    outcome: str,
    *,
    error: Optional[str] = None,
) -> None:
    payload: Dict[str, Any] = {
        "version": 1,
        "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "id": record.get("id"),
        "repo_root": record.get("repo_root"),
        "branch": record.get("branch"),
        "worktree_path": record.get("worktree_path"),
        "branch_outcome": record.get("branch_outcome"),
        "checkout_outcome": record.get("checkout_outcome"),
        "outcome": outcome,
    }
    if error:
        payload["error"] = error
    _atomic_write_json(state_dir / BRANCH_CLEANUP_DIR / "last-result.json", payload)


def _update_pending_cleanup_record(
    state_dir: Path,
    record: Mapping[str, Any],
    values: Mapping[str, Any],
) -> Mapping[str, Any]:
    cleanup_id = record.get("id")
    if not isinstance(cleanup_id, str):
        raise BootstrapError("pending cleanup record has no valid id")
    payload = {key: value for key, value in record.items() if key != "_path"}
    payload.update(values)
    _atomic_write_json(pending_cleanup_path(state_dir, cleanup_id), payload)
    result = dict(payload)
    result["_path"] = os.fspath(pending_cleanup_path(state_dir, cleanup_id))
    return result


def _resolve_pending_cleanup(
    state_dir: Path,
    record: Mapping[str, Any],
    outcome: str,
) -> None:
    _record_cleanup_result(state_dir, record, outcome)
    cleanup_id = record.get("id")
    if isinstance(cleanup_id, str):
        with contextlib.suppress(FileNotFoundError):
            pending_cleanup_path(state_dir, cleanup_id).unlink()


def _yes_no_unknown(value: Optional[bool]) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _safe_terminal_text(value: Any, limit: int = 240) -> str:
    text = str(value)
    sanitized = "".join(
        character if ord(character) >= 32 and ord(character) != 127 else " "
        for character in text
    )
    return " ".join(sanitized.split())[:limit]


@dataclasses.dataclass(frozen=True)
class StyledSegment:
    text: str
    role: str = "text"
    bold: bool = False
    dim: bool = False


def _display_width(value: str) -> int:
    width = 0
    for character in value:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
    return width


def _truncate_display(value: str, maximum: int) -> str:
    if maximum <= 0:
        return ""
    if _display_width(value) <= maximum:
        return value
    if maximum == 1:
        return "…"
    result: List[str] = []
    used = 0
    target = maximum - 1
    for character in value:
        character_width = 0 if unicodedata.combining(character) else (
            2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
        )
        if used + character_width > target:
            break
        result.append(character)
        used += character_width
    return "".join(result) + "…"


def _ansi_foreground(color: ColorValue) -> str:
    if color is None:
        return "\033[39m"
    if isinstance(color, int):
        return f"\033[{color}m"
    red, green, blue = color
    return f"\033[38;2;{red};{green};{blue}m"


def _style_text(
    value: str,
    palette: ThemePalette,
    role: str,
    *,
    enabled: bool,
    bold: bool = False,
    dim: bool = False,
) -> str:
    if not enabled:
        return value
    color = getattr(palette, role)
    attributes = ("\033[1m" if bold else "") + ("\033[2m" if dim else "")
    return f"{_ansi_foreground(color)}{attributes}{value}{ANSI_RESET}"


def _fit_segments(segments: Sequence[StyledSegment], maximum: int) -> List[StyledSegment]:
    fitted: List[StyledSegment] = []
    remaining = maximum
    for segment in segments:
        if remaining <= 0:
            break
        text = _truncate_display(segment.text, remaining)
        fitted.append(dataclasses.replace(segment, text=text))
        consumed = _display_width(text)
        remaining -= consumed
        if text != segment.text:
            break
    return fitted


def _flat_line(
    segments: Sequence[StyledSegment],
    width: int,
    palette: ThemePalette,
    *,
    color_enabled: bool,
    indent: int = 2,
) -> str:
    fitted = _fit_segments(segments, max(0, width - indent))
    content = "".join(
        _style_text(
            segment.text,
            palette,
            segment.role,
            enabled=color_enabled,
            bold=segment.bold,
            dim=segment.dim,
        )
        for segment in fitted
    )
    return " " * indent + content


def _flat_separator(
    width: int,
    palette: ThemePalette,
    *,
    color_enabled: bool,
) -> str:
    value = "  " + "─" * max(1, width - 4)
    return _style_text(value, palette, "subtle", enabled=color_enabled, dim=True)


def _action_row_width(actions: Sequence[ActionSpec]) -> int:
    groups = [_display_width(f"{key} {label}") for key, label, _role in actions]
    return sum(groups) + ACTION_GROUP_GAP * max(0, len(groups) - 1)


def _balanced_action_rows(
    actions: Sequence[ActionSpec],
    available_width: int,
) -> List[List[ActionSpec]]:
    """Use the fewest rows, then choose the most balanced contiguous split."""

    action_list = list(actions)
    if not action_list:
        return []
    for row_count in range(1, len(action_list) + 1):
        candidates: List[Tuple[Tuple[int, int, Tuple[int, ...]], List[List[ActionSpec]]]] = []
        for breaks in itertools.combinations(range(1, len(action_list)), row_count - 1):
            boundaries = (0,) + breaks + (len(action_list),)
            rows = [
                action_list[boundaries[index]:boundaries[index + 1]]
                for index in range(row_count)
            ]
            widths = tuple(_action_row_width(row) for row in rows)
            if all(width <= available_width for width in widths):
                score = (max(widths), max(widths) - min(widths), widths)
                candidates.append((score, rows))
        if candidates:
            return min(candidates, key=lambda candidate: candidate[0])[1]
    return [[action] for action in action_list]


def _action_segments(actions: Sequence[ActionSpec]) -> List[StyledSegment]:
    segments: List[StyledSegment] = []
    for index, (key, label, role) in enumerate(actions):
        if index:
            segments.append(StyledSegment(" " * ACTION_GROUP_GAP))
        segments.extend(
            [StyledSegment(key, role, bold=True), StyledSegment(f" {label}")]
        )
    return segments


def _render_action_rows(
    actions: Sequence[ActionSpec],
    width: int,
    palette: ThemePalette,
    *,
    color_enabled: bool,
) -> List[str]:
    return [
        _flat_line(
            _action_segments(row),
            width,
            palette,
            color_enabled=color_enabled,
        )
        for row in _balanced_action_rows(actions, max(1, width - 2))
    ]


def _supports_action_footer(decorated: bool, columns: int) -> bool:
    return decorated and columns >= MIN_ACTION_FOOTER_COLUMNS


def _friendly_path(value: Any) -> str:
    text = _safe_terminal_text(value)
    home = os.fspath(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + os.sep):
        return "~" + text[len(home):]
    return text


def _cleanup_status(inspection: BranchInspection) -> Tuple[str, str]:
    if inspection.protected:
        return "PROTECTED · deletion blocked", "danger"
    if inspection.used_by_worktrees:
        return "IN USE · deletion blocked", "warning"
    if inspection.merged_into_default is True:
        return f"MERGED · {inspection.default_ref or 'default'}", "positive"
    if inspection.merged_into_default is False:
        return f"UNMERGED · {inspection.default_ref or 'default'}", "warning"
    return "MERGE STATUS UNKNOWN", "warning"


def _plain_cleanup_dialog(
    inspection: BranchInspection,
    record: Mapping[str, Any],
    leftover: Optional[LeftoverInspection] = None,
) -> List[str]:
    lines = [
        "Herdr branch cleanup review",
        "",
        f"Repository: {_safe_terminal_text(inspection.repo_root)}",
        f"Removed worktree: {_safe_terminal_text(record.get('worktree_path', 'unknown'))}",
        f"Branch: {_safe_terminal_text(inspection.branch)}",
        f"Last commit: {_safe_terminal_text(inspection.last_commit or 'unknown')}",
        f"Default branch ref: {_safe_terminal_text(inspection.default_ref or 'unknown')}",
        f"Merged into default: {_yes_no_unknown(inspection.merged_into_default)}",
        f"Upstream: {_safe_terminal_text(inspection.upstream or 'none')}",
    ]
    if inspection.ahead is not None and inspection.behind is not None:
        lines.append(f"Upstream distance: ahead {inspection.ahead}, behind {inspection.behind}")
    lines.append("Remote branches are never deleted by this plugin.")
    if record.get("forced_worktree_removal"):
        lines.append("Warning: the worktree itself was removed with --force.")
    if leftover is not None and leftover.exists:
        suffix = "+" if leftover.truncated else ""
        lines.append(
            f"Leftover checkout files: {leftover.entry_count}{suffix}, "
            f"{_format_byte_count(leftover.total_bytes)}"
        )
        lines.extend(f"  {path}" for path in leftover.sample_paths[:3])
    if inspection.protected:
        lines.append("Deletion blocked: this is a protected/default branch.")
    if inspection.used_by_worktrees:
        lines.append("Deletion blocked: branch is used by another worktree:")
        lines.extend(f"  {_safe_terminal_text(path)}" for path in inspection.used_by_worktrees)
    return lines


def render_cleanup_dialog(
    inspection: BranchInspection,
    record: Mapping[str, Any],
    palette: ThemePalette,
    *,
    columns: int,
    decorated: bool,
    color_enabled: bool,
    leftover: Optional[LeftoverInspection] = None,
) -> List[str]:
    if not _supports_action_footer(decorated, columns):
        return _plain_cleanup_dialog(inspection, record, leftover)

    width = max(MIN_ACTION_FOOTER_COLUMNS, min(columns, 88))
    status, status_role = _cleanup_status(inspection)
    lines = [
        "",
        _flat_line(
            [StyledSegment("✓ ", "positive", bold=True), StyledSegment("Worktree removed", "text", bold=True)],
            width,
            palette,
            color_enabled=color_enabled,
        ),
        "",
    ]

    def detail(label: str, value: str, role: str = "text", bold: bool = False) -> None:
        lines.append(
            _flat_line(
                [StyledSegment(f"{label:<13}", "muted"), StyledSegment(value, role, bold=bold)],
                width,
                palette,
                color_enabled=color_enabled,
            )
        )

    detail("Repository", _friendly_path(inspection.repo_root))
    detail("Worktree", _friendly_path(record.get("worktree_path", "unknown")))
    detail("Branch", _safe_terminal_text(inspection.branch), "branch", True)
    detail("Status", status, status_role, True)
    detail("Last commit", _safe_terminal_text(inspection.last_commit or "unknown"))
    if inspection.upstream:
        upstream = _safe_terminal_text(inspection.upstream)
        if inspection.ahead is not None and inspection.behind is not None:
            upstream += f" · ahead {inspection.ahead}, behind {inspection.behind}"
        detail("Upstream", upstream)
    if leftover is not None and leftover.exists:
        suffix = "+" if leftover.truncated else ""
        detail(
            "Files remain",
            f"{leftover.entry_count}{suffix} files · {_format_byte_count(leftover.total_bytes)}",
            "warning",
            True,
        )

    lines.append("")
    if record.get("forced_worktree_removal"):
        lines.append(
            _flat_line(
                [StyledSegment("! Worktree removal was forced.", "peach", bold=True)],
                width,
                palette,
                color_enabled=color_enabled,
            )
        )
    if leftover is not None and leftover.exists:
        lines.append(
            _flat_line(
                [StyledSegment("! The removed checkout directory still contains files.", "warning", bold=True)],
                width,
                palette,
                color_enabled=color_enabled,
            )
        )
    if inspection.protected:
        advice = StyledSegment("This protected/default branch cannot be deleted here.", "danger", bold=True)
    elif inspection.used_by_worktrees:
        advice = StyledSegment("This branch is still checked out in another worktree.", "warning", bold=True)
    elif inspection.merged_into_default is True:
        advice = StyledSegment("This local branch can be deleted safely.", "positive", bold=True)
    elif inspection.merged_into_default is False:
        advice = StyledSegment("Unmerged commits may be lost by force deletion.", "warning", bold=True)
    else:
        advice = StyledSegment("Review this branch before choosing deletion.", "warning", bold=True)
    lines.append(_flat_line([advice], width, palette, color_enabled=color_enabled))
    lines.append(
        _flat_line(
            [StyledSegment("Remote branches are never changed.", "subtle", dim=True)],
            width,
            palette,
            color_enabled=color_enabled,
        )
    )
    for path in inspection.used_by_worktrees:
        lines.append(
            _flat_line(
                [StyledSegment("↳ ", "warning"), StyledSegment(_friendly_path(path), "muted")],
                width,
                palette,
                color_enabled=color_enabled,
            )
        )

    lines.extend(["", _flat_separator(width, palette, color_enabled=color_enabled)])
    blocked = inspection.protected or bool(inspection.used_by_worktrees)
    if blocked:
        actions: List[ActionSpec] = [
            ("ENTER", "Keep", "accent"),
            ("S", "Later", "accent"),
            ("Q", "Close", "accent"),
        ]
    else:
        actions = [
            ("ENTER", "Keep", "accent"),
            ("D", "Delete safely", "positive"),
            ("F", "Force…", "danger"),
            ("S", "Later", "accent"),
            ("Q", "Close", "accent"),
        ]
    lines.extend(_render_action_rows(actions, width, palette, color_enabled=color_enabled))
    lines.append("")
    return lines


def render_force_delete_dialog(
    inspection: BranchInspection,
    palette: ThemePalette,
    *,
    columns: int,
    decorated: bool,
    color_enabled: bool,
) -> List[str]:
    if not decorated or columns < 48:
        return [
            "Force deletion can discard commits not merged into the default branch.",
            f"Type '{inspection.branch}' to force-delete.",
        ]
    width = max(48, min(columns, 76))
    return [
        "",
        _flat_line(
            [StyledSegment("! Destructive action", "danger", bold=True)],
            width,
            palette,
            color_enabled=color_enabled,
        ),
        _flat_line(
            [StyledSegment("Commits not merged into the default branch can be lost.", "warning")],
            width,
            palette,
            color_enabled=color_enabled,
        ),
        "",
        _flat_line(
            [StyledSegment("Type the exact branch name to continue:", "muted")],
            width,
            palette,
            color_enabled=color_enabled,
        ),
        _flat_line(
            [StyledSegment(inspection.branch, "branch", bold=True)],
            width,
            palette,
            color_enabled=color_enabled,
        ),
        "",
    ]


def _read_utf8_character(file_descriptor: int) -> str:
    first = os.read(file_descriptor, 1)
    if not first:
        raise EOFError
    leading = first[0]
    if leading < 0x80:
        length = 1
    elif leading & 0xE0 == 0xC0:
        length = 2
    elif leading & 0xF0 == 0xE0:
        length = 3
    elif leading & 0xF8 == 0xF0:
        length = 4
    else:
        length = 1
    data = first
    while len(data) < length:
        continuation = os.read(file_descriptor, length - len(data))
        if not continuation:
            break
        data += continuation
    return data.decode("utf-8", errors="replace")


def read_single_key(
    *,
    input_fd: Optional[int] = None,
    output_stream: Optional[Any] = None,
) -> str:
    """Read one key without echo, restoring terminal state on every exit path."""

    file_descriptor = sys.stdin.fileno() if input_fd is None else input_fd
    stream = sys.stdout if output_stream is None else output_stream
    old_attributes = termios.tcgetattr(file_descriptor)
    new_attributes = list(old_attributes)
    new_attributes[6] = list(old_attributes[6])
    new_attributes[3] &= ~(termios.ICANON | termios.ECHO)
    new_attributes[6][termios.VMIN] = 1
    new_attributes[6][termios.VTIME] = 0
    stream.write("\033[?25l")
    stream.flush()
    try:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, new_attributes)
        character = _read_utf8_character(file_descriptor)
        if character == "\x03":
            raise KeyboardInterrupt
        if character == "\x1b":
            sequence = character
            while len(sequence) < 8 and select.select([file_descriptor], [], [], 0.03)[0]:
                sequence += os.read(file_descriptor, 1).decode("ascii", errors="ignore")
                if sequence.endswith(("A", "B", "C", "D", "~")):
                    break
            return {
                "\x1b[A": "up",
                "\x1b[B": "down",
                "\x1b[C": "right",
                "\x1b[D": "left",
                "\x1bOA": "up",
                "\x1bOB": "down",
                "\x1bOC": "right",
                "\x1bOD": "left",
                "\x1b[5~": "page-up",
                "\x1b[6~": "page-down",
            }.get(sequence, "escape")
        if character in ("\r", "\n"):
            return ""
        return character
    finally:
        try:
            termios.tcsetattr(file_descriptor, termios.TCSADRAIN, old_attributes)
        finally:
            stream.write("\033[?25h")
            stream.flush()


def normalize_cleanup_choice(value: str) -> str:
    choice = value.strip().casefold()
    # These jamo are produced by the same physical keys with a Korean layout.
    return {
        "escape": "q",
        "ㅇ": "d",
        "ㄹ": "f",
        "ㄴ": "s",
        "ㅂ": "q",
        "ㅏ": "k",
        "ㄱ": "r",
    }.get(choice, choice)


def review_pending_cleanups(
    state_dir: Path,
    *,
    cleanup_id: Optional[str] = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    effective_env = os.environ if env is None else env
    decorated = output_fn is print and sys.stdout.isatty()
    color_enabled = (
        decorated
        and "NO_COLOR" not in effective_env
        and effective_env.get("TERM", "") != "dumb"
    )
    theme_settings = load_theme_settings(effective_env)
    appearance = query_terminal_appearance() if decorated and theme_settings.auto_switch else None
    palette = resolve_theme_palette(theme_settings, appearance=appearance)
    columns = shutil.get_terminal_size((80, 24)).columns
    records = list_pending_cleanups(state_dir)
    if cleanup_id:
        records = [record for record in records if record.get("id") == cleanup_id]
    if not records:
        output_fn("No pending branch cleanups.")
        return 0

    for initial_record in records:
        record: Mapping[str, Any] = initial_record
        repo_value = record.get("repo_root")
        branch_value = record.get("branch")
        worktree_value = record.get("worktree_path")
        if (
            not isinstance(repo_value, str)
            or not isinstance(branch_value, str)
            or not isinstance(worktree_value, str)
        ):
            output_fn("Skipping an invalid pending branch cleanup record.")
            continue
        try:
            inspection = inspect_branch(Path(repo_value), branch_value)
        except BootstrapError as exc:
            output_fn(
                f"Cannot inspect {_safe_terminal_text(branch_value)}: "
                f"{_safe_terminal_text(exc)}"
            )
            output_fn("The request remains pending for later review.")
            continue
        leftover = inspect_leftover_path(Path(worktree_value))

        def display_cleanup(*notices: str) -> None:
            _clear_screen()
            for line in render_cleanup_dialog(
                inspection,
                record,
                palette,
                columns=columns,
                decorated=decorated,
                color_enabled=color_enabled,
                leftover=leftover,
            ):
                output_fn(line)
            for notice in notices:
                output_fn(notice)

        action_footer_visible = _supports_action_footer(decorated, columns)
        single_key_mode = action_footer_visible and input_fn is input and sys.stdin.isatty()
        branch_outcome = record.get("branch_outcome")
        deferred = False
        if not isinstance(branch_outcome, str):
            display_cleanup()
            if not inspection.exists:
                branch_outcome = "already_absent"
                record = _update_pending_cleanup_record(
                    state_dir,
                    record,
                    {"branch_outcome": branch_outcome},
                )
                output_fn("The local branch no longer exists.")
            else:
                while True:
                    if inspection.protected or inspection.used_by_worktrees:
                        prompt = "[K]eep branch (default)  [S]kip for later  [Q]uit: "
                    else:
                        prompt = "[K]eep (default)  [D]elete safely  [F]orce delete...  [S]kip  [Q]uit: "
                    if action_footer_visible and not single_key_mode:
                        prompt = _style_text(
                            "Select › ",
                            palette,
                            "accent",
                            enabled=color_enabled,
                            bold=True,
                        )
                    try:
                        if single_key_mode:
                            try:
                                choice = normalize_cleanup_choice(read_single_key())
                            except (OSError, ValueError, AttributeError):
                                single_key_mode = False
                                choice = normalize_cleanup_choice(input_fn(prompt))
                        else:
                            choice = normalize_cleanup_choice(input_fn(prompt))
                    except EOFError:
                        output_fn("No interactive input; request remains pending.")
                        return 0
                    if choice in ("", "k", "keep"):
                        branch_outcome = "kept"
                        record = _update_pending_cleanup_record(
                            state_dir, record, {"branch_outcome": branch_outcome}
                        )
                        output_fn(f"Kept local branch {inspection.branch}.")
                        break
                    if choice in ("s", "skip"):
                        output_fn("Request left pending for later review.")
                        deferred = True
                        break
                    if choice in ("q", "quit"):
                        return 0
                    if (
                        choice in ("d", "delete")
                        and not inspection.protected
                        and not inspection.used_by_worktrees
                    ):
                        try:
                            delete_local_branch(
                                inspection.repo_root,
                                inspection.branch,
                                force=False,
                                expected_oid=inspection.head_oid,
                            )
                        except BootstrapError as exc:
                            _record_cleanup_result(
                                state_dir, record, "safe_delete_failed", error=str(exc)
                            )
                            inspection = inspect_branch(inspection.repo_root, inspection.branch)
                            display_cleanup(
                                f"Safe deletion refused: {_safe_terminal_text(exc)}",
                                "The request remains pending. Use force only after reviewing unmerged commits.",
                            )
                            continue
                        branch_outcome = "deleted_safely"
                        record = _update_pending_cleanup_record(
                            state_dir, record, {"branch_outcome": branch_outcome}
                        )
                        output_fn(f"Deleted local branch {inspection.branch} safely.")
                        break
                    if (
                        choice in ("f", "force")
                        and not inspection.protected
                        and not inspection.used_by_worktrees
                    ):
                        _clear_screen()
                        for line in render_force_delete_dialog(
                            inspection,
                            palette,
                            columns=columns,
                            decorated=decorated,
                            color_enabled=color_enabled,
                        ):
                            output_fn(line)
                        confirmation_prompt = f"Type '{inspection.branch}' to force-delete: "
                        if decorated:
                            confirmation_prompt = _style_text(
                                "Branch name › ",
                                palette,
                                "danger",
                                enabled=color_enabled,
                                bold=True,
                            )
                        try:
                            confirmation = input_fn(confirmation_prompt).strip()
                        except EOFError:
                            output_fn("No confirmation; request remains pending.")
                            return 0
                        if confirmation != inspection.branch:
                            display_cleanup("Branch name did not match; nothing was deleted.")
                            continue
                        try:
                            delete_local_branch(
                                inspection.repo_root,
                                inspection.branch,
                                force=True,
                                expected_oid=inspection.head_oid,
                            )
                        except BootstrapError as exc:
                            _record_cleanup_result(
                                state_dir, record, "force_delete_failed", error=str(exc)
                            )
                            inspection = inspect_branch(inspection.repo_root, inspection.branch)
                            display_cleanup(f"Force deletion failed: {_safe_terminal_text(exc)}")
                            continue
                        branch_outcome = "deleted_forcibly"
                        record = _update_pending_cleanup_record(
                            state_dir, record, {"branch_outcome": branch_outcome}
                        )
                        output_fn(f"Force-deleted local branch {inspection.branch}.")
                        break
                    if single_key_mode:
                        sys.stdout.write("\a")
                        sys.stdout.flush()
                    else:
                        output_fn("Unknown or unavailable choice.")
        if deferred:
            continue

        leftover = inspect_leftover_path(Path(worktree_value))
        if not leftover.exists:
            record = _update_pending_cleanup_record(
                state_dir, record, {"checkout_outcome": "absent"}
            )
            _resolve_pending_cleanup(state_dir, record, str(branch_outcome))
            continue

        display_cleanup("Review the leftover checkout directory separately from the branch.")
        while True:
            prompt = "[K]eep files (default)  [R]emove files...  [S]kip  [Q]uit: "
            try:
                choice = normalize_cleanup_choice(input_fn(prompt))
            except EOFError:
                output_fn("No interactive input; request remains pending.")
                return 0
            if choice in ("", "k", "keep"):
                record = _update_pending_cleanup_record(
                    state_dir, record, {"checkout_outcome": "kept"}
                )
                _resolve_pending_cleanup(state_dir, record, "kept_with_leftover_files")
                output_fn(f"Kept leftover files at {worktree_value}.")
                break
            if choice in ("s", "skip"):
                output_fn("Leftover files remain pending for later review.")
                break
            if choice in ("q", "quit"):
                return 0
            if choice in ("r", "remove"):
                confirmation = input_fn(
                    f"Type '{Path(worktree_value).name}' to remove the leftover directory: "
                ).strip()
                if confirmation != Path(worktree_value).name:
                    output_fn("Directory name did not match; nothing was removed.")
                    continue
                try:
                    remove_leftover_worktree_files(
                        Path(repo_value),
                        Path(worktree_value),
                        worktree_root=load_worktree_directory(effective_env),
                        provenance_verified=bool(record.get("provenance_verified")),
                        expected_device=leftover.device,
                        expected_inode=leftover.inode,
                    )
                except BootstrapError as exc:
                    _record_cleanup_result(
                        state_dir, record, "checkout_delete_failed", error=str(exc)
                    )
                    output_fn(f"Leftover deletion failed: {_safe_terminal_text(exc)}")
                    leftover = inspect_leftover_path(Path(worktree_value))
                    continue
                record = _update_pending_cleanup_record(
                    state_dir, record, {"checkout_outcome": "removed"}
                )
                _resolve_pending_cleanup(state_dir, record, str(branch_outcome))
                output_fn(f"Removed leftover files at {worktree_value}.")
                break
            output_fn("Unknown choice.")
    return 0


def _herdr_command(env: Mapping[str, str]) -> Optional[str]:
    configured = env.get("HERDR_BIN_PATH")
    if configured:
        return configured
    return shutil.which("herdr")


def show_herdr_notification(
    title: str,
    body: str,
    env: Mapping[str, str],
    *,
    sound: str = "none",
) -> bool:
    herdr = _herdr_command(env)
    if not herdr:
        return False
    try:
        result = subprocess.run(
            [
                herdr,
                "notification",
                "show",
                title,
                "--body",
                body,
                "--sound",
                sound,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _repository_unavailable_body(reason: NothingToDo) -> str:
    if isinstance(reason, NotGitRepository):
        return "This workspace is not a Git repository. Open a Git-backed workspace and try again."
    return str(reason).rstrip(".") + "."


def notify_repository_unavailable(reason: NothingToDo, env: Mapping[str, str]) -> bool:
    return show_herdr_notification(
        "Worktree Bootstrap unavailable",
        _repository_unavailable_body(reason),
        env,
    )


def launch_management_popup(
    entrypoint: str,
    context: RepositoryContext,
    env: Mapping[str, str],
) -> None:
    sizes = {
        "manage": MANAGE_POPUP_SIZE,
        "setup-manage": SETUP_MANAGE_POPUP_SIZE,
        "inventory": INVENTORY_POPUP_SIZE,
    }
    if entrypoint not in sizes:
        raise BootstrapError(f"unknown management entrypoint: {entrypoint}")
    herdr = _herdr_command(env)
    if not herdr:
        raise BootstrapError("herdr is required to open the management popup")
    width, height = sizes[entrypoint]
    command = [
        herdr,
        "plugin",
        "pane",
        "open",
        "--plugin",
        PLUGIN_ID,
        "--entrypoint",
        entrypoint,
        "--placement",
        "popup",
        "--width",
        width,
        "--height",
        height,
        "--cwd",
        os.fspath(context.target),
        "--focus",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError("could not open the Worktree Bootstrap popup") from exc
    if result.returncode != 0:
        raise BootstrapError("could not open the Worktree Bootstrap popup")


def launch_management_ui(
    entrypoint: str,
    target_path: Path,
    env: Mapping[str, str],
) -> int:
    try:
        context = resolve_repository(target_path)
    except NothingToDo as exc:
        if not notify_repository_unavailable(exc, env):
            print(_repository_unavailable_body(exc), file=sys.stderr)
        return 0
    launch_management_popup(entrypoint, context, env)
    return 0


def launch_branch_cleanup_popup(
    cleanup_id: str,
    branch: str,
    env: Mapping[str, str],
) -> bool:
    herdr = _herdr_command(env)
    if not herdr:
        return False
    command = [
        herdr,
        "plugin",
        "pane",
        "open",
        "--plugin",
        PLUGIN_ID,
        "--entrypoint",
        "branch-cleanup",
        "--placement",
        "popup",
        "--width",
        "68%",
        "--height",
        "50%",
        "--focus",
        "--env",
        f"HERDR_BRANCH_CLEANUP_ID={cleanup_id}",
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode == 0:
        return True
    show_herdr_notification(
        "Branch cleanup pending",
        f"Review local branch {branch} from the Worktree Bootstrap action.",
        env,
        sound="request",
    )
    return False


def _lifecycle_environment(
    *,
    worktree_path: Path,
    repo_root: Path,
    branch: Optional[str],
    head_oid: Optional[str],
    forced: bool,
    leftover: bool,
) -> Mapping[str, str]:
    return {
        "HERDR_WORKTREE_PATH": os.fspath(worktree_path),
        "HERDR_PRIMARY_PATH": os.fspath(repo_root),
        "HERDR_WORKTREE_BRANCH": branch or "",
        "HERDR_WORKTREE_HEAD": head_oid or "",
        "HERDR_REMOVAL_FORCED": "1" if forced else "0",
        "HERDR_LEFTOVER_DETECTED": "1" if leftover else "0",
    }


def _update_worktree_mapping(
    state_dir: Path,
    worktree_path: Path,
    values: Mapping[str, Any],
) -> Mapping[str, Any]:
    existing = load_worktree_mapping(state_dir, worktree_path)
    payload: Dict[str, Any] = dict(existing) if isinstance(existing, dict) else {
        "version": 2,
        "target": os.fspath(_absolute_path(worktree_path)),
    }
    payload.update(values)
    _atomic_write_json(worktree_map_path(state_dir, worktree_path), payload)
    return payload


def _removed_lifecycle_context(
    env: Mapping[str, str],
    state_dir: Path,
) -> Tuple[Path, Path, Optional[str], bool, Mapping[str, Any]]:
    envelope = _decode_json_env(env, "HERDR_PLUGIN_EVENT_JSON")
    data = envelope.get("data") if isinstance(envelope, dict) else None
    worktree = data.get("worktree") if isinstance(data, dict) else None
    raw_path = worktree.get("path") if isinstance(worktree, dict) else None
    if not isinstance(raw_path, str) or not raw_path:
        raise BootstrapError("worktree.removed event has no worktree path")
    path = _absolute_path(Path(raw_path))
    mapping = load_worktree_mapping(state_dir, path) or {}
    raw_repo: Any = mapping.get("repo_root")
    workspace = data.get("workspace") if isinstance(data, dict) else None
    provenance = workspace.get("worktree") if isinstance(workspace, dict) else None
    if isinstance(provenance, dict) and isinstance(provenance.get("repo_root"), str):
        raw_repo = provenance["repo_root"]
    if not isinstance(raw_repo, str):
        raise BootstrapError("cannot identify the primary checkout for removed worktree")
    repo_root = _validate_cleanup_repository(_canonical(Path(raw_repo)))
    raw_branch = worktree.get("branch") if isinstance(worktree, dict) else None
    branch = _normalize_local_branch(raw_branch) if isinstance(raw_branch, str) else None
    forced = bool(data.get("forced", False)) if isinstance(data, dict) else False
    return path, repo_root, branch, forced, mapping


def run_after_remove_lifecycle(
    env: Mapping[str, str],
    state_dir: Path,
) -> int:
    path, repo_root, branch, forced, mapping = _removed_lifecycle_context(env, state_dir)
    generation = mapping.get("recorded_at")
    if generation and mapping.get("after_remove_generation") == generation:
        return int(mapping.get("after_remove_completed_count", 0) or 0)
    commands = load_lifecycle_commands(repo_root)["after_remove"]
    leftover = inspect_leftover_path(path)
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    _update_worktree_mapping(
        state_dir,
        path,
        {
            "after_remove_status": "running",
            "after_remove_started_at": started_at,
            "after_remove_generation": generation,
        },
    )
    try:
        count = run_lifecycle_commands(
            repo_root,
            commands,
            phase="after_remove",
            environment=_lifecycle_environment(
                worktree_path=path,
                repo_root=repo_root,
                branch=branch,
                head_oid=mapping.get("created_head_oid") if isinstance(mapping.get("created_head_oid"), str) else None,
                forced=forced,
                leftover=leftover.exists,
            ),
        )
    except BootstrapError as exc:
        _update_worktree_mapping(
            state_dir,
            path,
            {
                "after_remove_status": "failed",
                "after_remove_error": str(exc),
                "after_remove_finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "after_remove_generation": generation,
            },
        )
        raise
    _update_worktree_mapping(
        state_dir,
        path,
        {
            "after_remove_status": "success",
            "after_remove_completed_count": count,
            "after_remove_finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "after_remove_generation": generation,
        },
    )
    return count


def handle_branch_cleanup_event(
    env: Mapping[str, str],
    state_dir: Path,
    *,
    open_popup: bool = True,
) -> Mapping[str, Any]:
    try:
        pending = queue_branch_cleanup(env, state_dir)
    except NothingToDo:
        try:
            run_after_remove_lifecycle(env, state_dir)
        except BootstrapError as exc:
            print(f"after-remove lifecycle failed: {exc}", file=sys.stderr, flush=True)
        raise
    try:
        run_after_remove_lifecycle(env, state_dir)
    except BootstrapError as exc:
        print(f"after-remove lifecycle failed: {exc}", file=sys.stderr, flush=True)
    popup_opened = False
    if open_popup:
        popup_opened = launch_branch_cleanup_popup(str(pending["id"]), str(pending["branch"]), env)
    result = dict(pending)
    result["popup_opened"] = popup_opened
    print(
        f"queued branch cleanup for {pending['branch']}; "
        f"popup={'opened' if popup_opened else 'pending'}",
        flush=True,
    )
    return result


def target_key(context: RepositoryContext) -> str:
    value = f"{context.common_git_dir}\0{context.target}".encode("utf-8", "surrogateescape")
    return hashlib.sha256(value).hexdigest()[:24]


def _active_lifecycle_environment(
    context: RepositoryContext,
    state_dir: Path,
) -> Mapping[str, str]:
    mapping = load_worktree_mapping(state_dir, context.target) or {}
    branch_result = _run_git(
        context.target,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        check=False,
    )
    head_result = _run_git(context.target, ["rev-parse", "--verify", "HEAD"], check=False)
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    head_oid = head_result.stdout.strip() if head_result.returncode == 0 else None
    if not branch and isinstance(mapping.get("branch"), str):
        branch = str(mapping["branch"])
    if not head_oid and isinstance(mapping.get("created_head_oid"), str):
        head_oid = str(mapping["created_head_oid"])
    return _lifecycle_environment(
        worktree_path=context.target,
        repo_root=context.source,
        branch=branch,
        head_oid=head_oid,
        forced=False,
        leftover=False,
    )


class TargetLock:
    def __init__(self, state_dir: Path, key: str) -> None:
        self.state_dir = state_dir
        self.path = state_dir / f"target-{key}.lock"
        self._handle = None

    def __enter__(self) -> "TargetLock":
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise BootstrapError("another bootstrap operation is already running for this target", 75) from exc
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(f"pid={os.getpid()}\n")
        self._handle.flush()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def state_path(state_dir: Path, context: RepositoryContext) -> Path:
    return state_dir / f"last-run-{target_key(context)}.json"


def write_run_state(state_dir: Path, context: RepositoryContext, result: Mapping[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(result)
    payload["target"] = os.fspath(context.target)
    payload["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    path = state_path(state_dir, context)
    fd, temp_name = tempfile.mkstemp(prefix=".last-run-", suffix=".json", dir=os.fspath(state_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def read_run_state(state_dir: Path, context: RepositoryContext) -> Optional[Mapping[str, Any]]:
    path = state_path(state_dir, context)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {"status": "unreadable"}
    return document if isinstance(document, dict) else {"status": "unreadable"}


def execute_action(action: str, context: RepositoryContext, state_dir: Path) -> Mapping[str, Any]:
    if context.target_is_primary:
        raise NothingToDo("the primary checkout is never bootstrapped")
    copy_config_exists = (context.source / COPY_LIST).exists()
    setup_config_exists = (context.source / SETUP_FILE).exists()
    if action == "bootstrap" and not copy_config_exists and not setup_config_exists:
        raise NothingToDo("no bootstrap configuration found; nothing was changed")
    if action == "sync" and not copy_config_exists:
        raise NothingToDo(f"{COPY_LIST} is absent; nothing was changed")
    if action == "setup" and not setup_config_exists:
        raise NothingToDo(f"{SETUP_FILE} is absent; nothing was changed")

    result: Dict[str, Any] = {
        "action": action,
        "status": "running",
        "copied_count": 0,
        "setup_completed_count": 0,
        "skipped": {},
    }
    with TargetLock(state_dir, target_key(context)):
        cleanup_stale_transactions(context.target)
        try:
            if action in ("bootstrap", "sync"):
                paths = read_copy_list(context.source)
                statuses = classify_copy_entries(context.source, context.target, paths)
                skipped: Dict[str, int] = {}
                for status_value in statuses:
                    if not status_value.copyable:
                        skipped[status_value.eligibility] = skipped.get(status_value.eligibility, 0) + 1
                        print(f"skip {status_value.path}: {status_value.detail}", flush=True)
                result["skipped"] = skipped
                result["copied_count"] = sync_paths(context, statuses)
                print(f"copied {result['copied_count']} item(s)", flush=True)
            if action in ("bootstrap", "setup"):
                commands = load_setup_commands(context.source)
                result["setup_completed_count"] = run_lifecycle_commands(
                    context.target,
                    commands,
                    phase="on_created",
                    environment=_active_lifecycle_environment(context, state_dir),
                )
                print(f"completed {result['setup_completed_count']} setup command(s)", flush=True)
            result["status"] = "success"
            write_run_state(state_dir, context, result)
            if action == "bootstrap" and load_worktree_mapping(state_dir, context.target) is not None:
                _update_worktree_mapping(
                    state_dir,
                    context.target,
                    {
                        "on_created_status": "success",
                        "on_created_completed_count": result["setup_completed_count"],
                        "on_created_finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    },
                )
            return result
        except BaseException as exc:
            if not isinstance(exc, KeyboardInterrupt):
                result["status"] = "failed"
                result["error"] = str(exc)
                if isinstance(exc, SetupCommandError):
                    result["failed_command"] = {
                        "index": exc.command_index,
                        "name": exc.command_name,
                    }
                    result["failure_kind"] = exc.failure_kind
                    if exc.command_exit_code is not None:
                        result["exit_code"] = exc.command_exit_code
                with contextlib.suppress(OSError):
                    write_run_state(state_dir, context, result)
                if action == "bootstrap" and load_worktree_mapping(state_dir, context.target) is not None:
                    with contextlib.suppress(OSError):
                        _update_worktree_mapping(
                            state_dir,
                            context.target,
                            {
                                "on_created_status": "failed",
                                "on_created_error": str(exc),
                                "on_created_finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                            },
                        )
            raise


def status_lines(context: RepositoryContext, state_dir: Path) -> Tuple[List[str], bool]:
    lines = [
        f"Source: {context.source}",
        f"Target: {context.target}",
    ]
    invalid = False
    try:
        paths = read_copy_list(context.source)
        statuses = classify_copy_entries(context.source, context.target, paths)
    except BootstrapError as exc:
        lines.append(f"Copy list: INVALID ({exc})")
        statuses = []
        invalid = True
    else:
        if not (context.source / COPY_LIST).exists():
            lines.append(f"Copy list: absent ({COPY_LIST})")
        elif not statuses:
            lines.append("Copy list: valid, no entries")
        else:
            lines.append(f"Copy list: valid, {len(statuses)} entry/entries")
        for entry in statuses:
            source_word = "present" if entry.source_exists else "missing"
            target_word = "present" if entry.target_exists else "missing"
            lines.append(
                f"  {entry.path}: {entry.eligibility}; source={source_word}; "
                f"target={target_word}; {entry.detail}"
            )

    try:
        lifecycle = load_lifecycle_commands(context.source)
        commands = lifecycle["on_created"]
    except BootstrapError as exc:
        lines.append(f"Setup: INVALID ({exc})")
        invalid = True
    else:
        if not (context.source / SETUP_FILE).exists():
            lines.append(f"Setup: absent ({SETUP_FILE})")
        else:
            lines.append(f"Setup: valid, {len(commands)} command(s)")
            for command in commands:
                lines.append(f"  {command.name}: {list(command.argv)!r}, timeout={command.timeout_seconds}s")
            lines.append(
                "Lifecycle: "
                f"before_remove={len(lifecycle['before_remove'])}, "
                f"after_remove={len(lifecycle['after_remove'])}"
            )

    last = read_run_state(state_dir, context)
    if last is None:
        lines.append("Last run: none")
    else:
        summary = f"status={last.get('status', 'unknown')}"
        if last.get("action"):
            summary += f", action={last['action']}"
        if last.get("copied_count") is not None:
            summary += f", copied={last.get('copied_count', 0)}"
        if last.get("setup_completed_count") is not None:
            summary += f", setup={last.get('setup_completed_count', 0)}"
        failed_command = last.get("failed_command")
        if isinstance(failed_command, dict) and failed_command.get("name"):
            summary += f", failed_command={failed_command['name']}"
        if last.get("exit_code") is not None:
            summary += f", exit_code={last['exit_code']}"
        if last.get("error"):
            summary += f", error={last['error']}"
        lines.append(f"Last run: {summary}")
    pending_count = 0
    for pending in list_pending_cleanups(state_dir):
        repo_value = pending.get("repo_root")
        if isinstance(repo_value, str) and _canonical(Path(repo_value)) == context.source:
            pending_count += 1
    lines.append(f"Pending branch cleanups: {pending_count}")
    return lines, invalid


def _exclude_path(context: RepositoryContext) -> Path:
    return context.common_git_dir / "info" / "exclude"


def ensure_control_excluded(context: RepositoryContext, relative: Path) -> None:
    exclude_path = _exclude_path(context)
    entry = "/" + relative.as_posix()
    try:
        current = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        existing = {line.strip() for line in current.splitlines()}
        if entry in existing:
            return
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        with exclude_path.open("a", encoding="utf-8") as handle:
            if current and not current.endswith("\n"):
                handle.write("\n")
            handle.write(entry + "\n")
    except OSError as exc:
        raise BootstrapError(f"cannot update {exclude_path}: {exc}") from exc


def write_copy_list(context: RepositoryContext, paths: Sequence[str]) -> None:
    paths = validate_copy_paths(paths)
    control_path = context.source / COPY_LIST
    control_path.parent.mkdir(parents=True, exist_ok=True)
    ensure_control_excluded(context, COPY_LIST)
    contents = "# Local ignored paths copied into new worktrees.\n"
    if paths:
        contents += "\n".join(paths) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".worktree-copy.", dir=os.fspath(control_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, control_path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _normalize_setup_commands(commands: Sequence[SetupCommand]) -> List[SetupCommand]:
    normalized: List[SetupCommand] = []
    for index, command in enumerate(commands, 1):
        label = f"setup command {index}"
        if not isinstance(command, SetupCommand):
            raise BootstrapError(f"{label} must be a SetupCommand")
        name = command.name.strip()
        argv = tuple(command.argv)
        timeout = command.timeout_seconds
        if not name:
            raise BootstrapError(f"{label} name must be a non-empty string")
        if not argv or not all(isinstance(arg, str) for arg in argv) or not argv[0]:
            raise BootstrapError(f"{label} argv must be a non-empty array of strings")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise BootstrapError(f"{label} timeout_seconds must be a positive integer")
        normalized.append(SetupCommand(name=name, argv=argv, timeout_seconds=timeout))
    return normalized


def write_setup_commands(
    context: RepositoryContext,
    commands: Sequence[SetupCommand],
) -> None:
    lifecycle = load_lifecycle_commands(context.source)
    updated = {phase: list(lifecycle[phase]) for phase in LIFECYCLE_PHASES}
    updated["on_created"] = _normalize_setup_commands(commands)
    write_lifecycle_commands(context, updated)


def write_lifecycle_commands(
    context: RepositoryContext,
    hooks: Mapping[str, Sequence[SetupCommand]],
) -> None:
    unknown = set(hooks) - set(LIFECYCLE_PHASES)
    if unknown:
        raise BootstrapError(f"unknown lifecycle phase: {sorted(unknown)[0]}")
    normalized = {
        phase: _normalize_setup_commands(hooks.get(phase, []))
        for phase in LIFECYCLE_PHASES
    }
    ensure_control_excluded(context, SETUP_FILE)
    _atomic_write_json(
        context.source / SETUP_FILE,
        {
            "version": 2,
            "hooks": {
                phase: [
                    {
                        "name": command.name,
                        "argv": list(command.argv),
                        "timeout_seconds": command.timeout_seconds,
                    }
                    for command in normalized[phase]
                ]
                for phase in LIFECYCLE_PHASES
            },
        },
    )


def remove_setup_configuration(context: RepositoryContext) -> None:
    lifecycle = load_lifecycle_commands(context.source)
    if lifecycle["before_remove"] or lifecycle["after_remove"]:
        updated = {phase: list(lifecycle[phase]) for phase in LIFECYCLE_PHASES}
        updated["on_created"] = []
        write_lifecycle_commands(context, updated)
        return
    with contextlib.suppress(FileNotFoundError):
        (context.source / SETUP_FILE).unlink()


def _prompt_default(
    prompt: str,
    default: str,
    *,
    input_fn: Optional[Callable[[str], str]] = None,
) -> str:
    reader = input if input_fn is None else input_fn
    value = reader(f"{prompt} [{default}]: ").strip()
    return default if not value else value


def _parse_setup_argv(value: str) -> Tuple[str, ...]:
    try:
        argv = tuple(shlex.split(value, comments=False, posix=True))
    except ValueError as exc:
        raise BootstrapError(f"invalid command arguments: {exc}") from exc
    if not argv or not argv[0]:
        raise BootstrapError("command arguments must include an executable")
    return argv


def _prompt_setup_command(
    existing: Optional[SetupCommand] = None,
    *,
    input_fn: Optional[Callable[[str], str]] = None,
) -> SetupCommand:
    name_default = existing.name if existing else "Install dependencies"
    argv_default = shlex.join(existing.argv) if existing else "npm ci"
    timeout_default = str(existing.timeout_seconds) if existing else "900"
    name = _prompt_default("Name", name_default, input_fn=input_fn)
    argv = _parse_setup_argv(
        _prompt_default("Command argv (quotes supported)", argv_default, input_fn=input_fn)
    )
    timeout_value = _prompt_default("Timeout seconds", timeout_default, input_fn=input_fn)
    try:
        timeout = int(timeout_value)
    except ValueError as exc:
        raise BootstrapError("timeout must be a positive integer") from exc
    if timeout <= 0:
        raise BootstrapError("timeout must be a positive integer")
    return SetupCommand(name=name, argv=argv, timeout_seconds=timeout)


def _prompt_setup_index(commands: Sequence[SetupCommand], prompt: str) -> Optional[int]:
    if not commands:
        print("No setup commands configured.")
        input("Press Enter.")
        return None
    raw = input(prompt).strip()
    try:
        index = int(raw) - 1
    except ValueError:
        print("Invalid command number.")
        input("Press Enter.")
        return None
    if index < 0 or index >= len(commands):
        print("Invalid command number.")
        input("Press Enter.")
        return None
    return index


def _ui_appearance(
    env: Mapping[str, str],
) -> Tuple[ThemePalette, bool, bool]:
    decorated = sys.stdout.isatty()
    color_enabled = (
        decorated
        and "NO_COLOR" not in env
        and env.get("TERM", "") != "dumb"
    )
    settings = load_theme_settings(env)
    appearance = query_terminal_appearance() if decorated and settings.auto_switch else None
    return resolve_theme_palette(settings, appearance=appearance), decorated, color_enabled


def _ui_width(columns: int) -> int:
    return max(28, min(columns, 96))


class TerminalScreenRenderer:
    """Batch terminal refreshes so navigation never exposes a blank frame."""

    def __init__(
        self,
        *,
        decorated: bool,
        stream: Optional[Any] = None,
    ) -> None:
        self.decorated = decorated
        self.stream = sys.stdout if stream is None else stream
        self.initialized = False

    def draw(self, lines: Sequence[str]) -> None:
        if not self.decorated:
            for line in lines:
                print(line, file=self.stream)
            return
        prefix = "\033[2J\033[H" if not self.initialized else "\033[H"
        self.stream.write(prefix + "\n".join(lines) + "\033[J")
        self.stream.flush()
        self.initialized = True

    def invalidate(self) -> None:
        self.initialized = False


def _section_heading(
    title: str,
    summary: str,
    width: int,
    palette: ThemePalette,
    *,
    color_enabled: bool,
) -> str:
    return _flat_line(
        [
            StyledSegment(title.upper(), "accent", bold=True),
            StyledSegment("  "),
            StyledSegment(summary, "muted"),
        ],
        width,
        palette,
        color_enabled=color_enabled,
    )


def _limited_count(total: int, limit: int) -> Optional[str]:
    remaining = total - limit
    return f"… {remaining} more" if remaining > 0 else None


@dataclasses.dataclass(frozen=True)
class ScrollableScreen:
    lines: Tuple[str, ...]
    offset: int
    maximum_offset: int
    page_size: int


def _scroll_indicator(
    offset: int,
    page_size: int,
    total: int,
    width: int,
    palette: ThemePalette,
    *,
    color_enabled: bool,
) -> str:
    start = offset + 1
    end = min(total, offset + page_size)
    if width < 44:
        segments = [
            StyledSegment("SCROLL ", "accent", bold=True),
            StyledSegment(f"{start}–{end}/{total}", "text", bold=True),
            StyledSegment("  K↑ J↓", "muted"),
        ]
    else:
        segments = [
            StyledSegment("SCROLL  ", "accent", bold=True),
            StyledSegment(f"{start}–{end} / {total}", "text", bold=True),
            StyledSegment("  ·  K/↑ up  J/↓ down", "muted"),
        ]
    if width >= 64:
        track_size = min(12, max(6, width - 50))
        maximum_offset = max(1, total - page_size)
        marker = min(track_size - 1, round(offset * (track_size - 1) / maximum_offset))
        track = "".join("█" if index == marker else "░" for index in range(track_size))
        segments.extend([StyledSegment("  ", "subtle"), StyledSegment(track, "accent")])
    return _flat_line(segments, width, palette, color_enabled=color_enabled)


def _compose_scrollable_screen(
    header: Sequence[str],
    body: Sequence[str],
    actions: Sequence[ActionSpec],
    width: int,
    rows: int,
    palette: ThemePalette,
    *,
    color_enabled: bool,
    requested_offset: int,
    notice: Optional[Tuple[str, str]],
) -> ScrollableScreen:
    footer: List[str] = []
    if notice:
        footer.extend(
            [
                "",
                _flat_line(
                    [StyledSegment(notice[0], notice[1], bold=notice[1] in ("positive", "danger"))],
                    width,
                    palette,
                    color_enabled=color_enabled,
                ),
            ]
        )
    footer.extend(
        ["", _flat_separator(width, palette, color_enabled=color_enabled)]
    )
    footer.extend(_render_action_rows(actions, width, palette, color_enabled=color_enabled))
    footer.append("")

    page_size = max(1, rows - len(header) - len(footer))
    scrollable = len(body) > page_size
    if scrollable:
        page_size = max(1, page_size - 1)
    maximum_offset = max(0, len(body) - page_size)
    offset = max(0, min(requested_offset, maximum_offset))
    visible_body = list(body[offset : offset + page_size])
    if scrollable:
        footer.insert(
            0,
            _scroll_indicator(
                offset,
                page_size,
                len(body),
                width,
                palette,
                color_enabled=color_enabled,
            ),
        )
    return ScrollableScreen(
        tuple([*header, *visible_body, *footer]),
        offset,
        maximum_offset,
        page_size,
    )


def _render_setup_rows(
    commands: Sequence[SetupCommand],
    width: int,
    palette: ThemePalette,
    *,
    color_enabled: bool,
    limit: int,
) -> List[str]:
    if not commands:
        return [
            _flat_line(
                [StyledSegment("No setup commands configured.", "subtle", dim=True)],
                width,
                palette,
                color_enabled=color_enabled,
            )
        ]
    lines: List[str] = []
    compact = width < 58
    for index, command in enumerate(commands[:limit], 1):
        command_line = shlex.join(command.argv)
        lines.append(
            _flat_line(
                [
                    StyledSegment(f"{index:>2}  ", "subtle"),
                    StyledSegment(command.name, "text", bold=True),
                ],
                width,
                palette,
                color_enabled=color_enabled,
            )
        )
        metadata = command_line if compact else f"{command_line}  ·  {command.timeout_seconds}s timeout"
        lines.append(
            _flat_line(
                [StyledSegment(f"    {metadata}", "muted", dim=True)],
                width,
                palette,
                color_enabled=color_enabled,
            )
        )
        if compact:
            lines.append(
                _flat_line(
                    [StyledSegment(f"    timeout {command.timeout_seconds}s", "subtle", dim=True)],
                    width,
                    palette,
                    color_enabled=color_enabled,
                )
            )
    remaining = _limited_count(len(commands), limit)
    if remaining:
        lines.append(
            _flat_line(
                [StyledSegment(remaining, "subtle", dim=True)],
                width,
                palette,
                color_enabled=color_enabled,
            )
        )
    return lines


def _render_setup_management_view(
    context: RepositoryContext,
    commands: Sequence[SetupCommand],
    palette: ThemePalette,
    *,
    columns: int,
    rows: int,
    decorated: bool,
    color_enabled: bool,
    scroll_offset: int = 0,
    notice: Optional[Tuple[str, str]] = None,
) -> ScrollableScreen:
    width = _ui_width(columns)
    header = [
        "",
        _flat_line(
            [
                StyledSegment("◆ ", "accent", bold=True),
                StyledSegment("Worktree bootstrap", "text", bold=True),
                StyledSegment("  /  Setup", "muted"),
            ],
            width,
            palette,
            color_enabled=color_enabled,
        ),
        _flat_line(
            [
                StyledSegment("Primary  ", "subtle"),
                StyledSegment(_friendly_path(context.source), "muted"),
            ],
            width,
            palette,
            color_enabled=color_enabled,
        ),
    ]
    body = [
        "",
        _section_heading(
            "Automation",
            f"{len(commands)} command{'s' if len(commands) != 1 else ''}",
            width,
            palette,
            color_enabled=color_enabled,
        ),
    ]
    body.extend(
        _render_setup_rows(
            commands,
            width,
            palette,
            color_enabled=color_enabled,
            limit=max(1, len(commands)),
        )
    )
    if not commands:
        body.append(
            _flat_line(
                [StyledSegment("Runs after local files are copied into a new worktree.", "subtle", dim=True)],
                width,
                palette,
                color_enabled=color_enabled,
            )
        )
    actions: List[ActionSpec] = [
        ("N", "New", "accent"),
        ("E", "Edit", "accent"),
        ("X", "Delete", "danger"),
        ("U", "Up", "accent"),
        ("M", "Move down", "accent"),
        ("R", "Run now", "positive"),
        ("Z", "Remove config", "warning"),
        ("Q", "Back", "accent"),
    ]
    return _compose_scrollable_screen(
        header,
        body,
        actions,
        width,
        rows,
        palette,
        color_enabled=color_enabled,
        requested_offset=scroll_offset,
        notice=notice,
    )


def render_setup_management_screen(
    context: RepositoryContext,
    commands: Sequence[SetupCommand],
    palette: ThemePalette,
    *,
    columns: int,
    rows: int,
    decorated: bool,
    color_enabled: bool,
    scroll_offset: int = 0,
    notice: Optional[Tuple[str, str]] = None,
) -> List[str]:
    return list(
        _render_setup_management_view(
            context,
            commands,
            palette,
            columns=columns,
            rows=rows,
            decorated=decorated,
            color_enabled=color_enabled,
            scroll_offset=scroll_offset,
            notice=notice,
        ).lines
    )


_MANAGE_KEY_ALIASES: Mapping[str, str] = {
    "ㅁ": "a",
    "ㅑ": "i",
    "ㅇ": "d",
    "ㅊ": "c",
    "ㄴ": "s",
    "ㅅ": "t",
    "ㅍ": "v",
    "ㄱ": "r",
    "ㅂ": "q",
    "ㅜ": "n",
    "ㄷ": "e",
    "ㅌ": "x",
    "ㅕ": "u",
    "ㅓ": "j",
    "ㅏ": "k",
    "ㅡ": "m",
    "ㅋ": "z",
    "ㅛ": "y",
    "up": "k",
    "down": "j",
    "page-up": "page-up",
    "page-down": "page-down",
    "escape": "q",
}


def normalize_manage_choice(value: str) -> str:
    choice = value.strip().casefold()
    return _MANAGE_KEY_ALIASES.get(choice, choice)


def _read_manage_choice(*, single_key_mode: bool, prompt: str = "Select › ") -> str:
    if single_key_mode:
        try:
            return normalize_manage_choice(read_single_key())
        except (OSError, ValueError, AttributeError):
            pass
    return normalize_manage_choice(input(prompt))


def _confirm_manage_action(prompt: str, *, single_key_mode: bool) -> bool:
    if single_key_mode:
        print(prompt, end="", flush=True)
        try:
            choice = normalize_manage_choice(read_single_key())
        except (OSError, ValueError, AttributeError):
            choice = normalize_manage_choice(input())
        else:
            print()
        return choice == "y"
    return normalize_manage_choice(input(prompt)) == "y"


def _show_manage_form(
    title: str,
    hint: str,
    palette: ThemePalette,
    *,
    columns: int,
    color_enabled: bool,
) -> None:
    width = _ui_width(columns)
    _clear_screen()
    print()
    print(
        _flat_line(
            [
                StyledSegment("◆ ", "accent", bold=True),
                StyledSegment(title, "text", bold=True),
            ],
            width,
            palette,
            color_enabled=color_enabled,
        )
    )
    print(
        _flat_line(
            [StyledSegment(hint, "muted", dim=True)],
            width,
            palette,
            color_enabled=color_enabled,
        )
    )
    print()


def _show_setup_choices(
    commands: Sequence[SetupCommand],
    palette: ThemePalette,
    *,
    columns: int,
    color_enabled: bool,
) -> None:
    width = _ui_width(columns)
    for index, command in enumerate(commands, 1):
        print(
            _flat_line(
                [
                    StyledSegment(f"{index:>2}  ", "subtle"),
                    StyledSegment(command.name, "text", bold=True),
                    StyledSegment(f"  {shlex.join(command.argv)}", "muted", dim=True),
                ],
                width,
                palette,
                color_enabled=color_enabled,
            )
        )
    if commands:
        print()


def manage_setup_commands(
    context: RepositoryContext,
    state_dir: Path,
    commands: Sequence[SetupCommand],
    *,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    effective_env = os.environ if env is None else env
    palette, decorated, color_enabled = _ui_appearance(effective_env)
    single_key_mode = decorated and sys.stdin.isatty()
    current = list(commands)
    scroll_offset = 0
    notice: Optional[Tuple[str, str]] = None
    renderer = TerminalScreenRenderer(decorated=decorated)
    while True:
        terminal_size = shutil.get_terminal_size((80, 24))
        screen = _render_setup_management_view(
            context,
            current,
            palette,
            columns=terminal_size.columns,
            rows=terminal_size.lines,
            decorated=decorated,
            color_enabled=color_enabled,
            scroll_offset=scroll_offset,
            notice=notice,
        )
        scroll_offset = screen.offset
        renderer.draw(screen.lines)
        notice = None
        try:
            choice = _read_manage_choice(single_key_mode=single_key_mode)
        except EOFError:
            return 0
        if choice == "q":
            return 0
        if choice == "":
            continue
        if choice == "k":
            scroll_offset = max(0, scroll_offset - 1)
            continue
        if choice == "j":
            scroll_offset = min(screen.maximum_offset, scroll_offset + 1)
            continue
        if choice == "page-up":
            scroll_offset = max(0, scroll_offset - screen.page_size)
            continue
        if choice == "page-down":
            scroll_offset = min(screen.maximum_offset, scroll_offset + screen.page_size)
            continue
        if choice == "n":
            try:
                _show_manage_form(
                    "New setup command",
                    "Commands run without a shell in the target worktree.",
                    palette,
                    columns=terminal_size.columns,
                    color_enabled=color_enabled,
                )
                command = _prompt_setup_command()
                write_setup_commands(context, [*current, command])
                current.append(command)
                notice = (f"✓ Added {command.name}", "positive")
            except BootstrapError as exc:
                notice = (f"Could not save: {exc}", "danger")
            continue
        if choice == "e":
            _show_manage_form(
                "Edit setup command",
                "Choose the command number, then keep or replace each value.",
                palette,
                columns=terminal_size.columns,
                color_enabled=color_enabled,
            )
            _show_setup_choices(
                current,
                palette,
                columns=terminal_size.columns,
                color_enabled=color_enabled,
            )
            index = _prompt_setup_index(current, "Command number to edit: ")
            if index is None:
                continue
            try:
                command = _prompt_setup_command(current[index])
                updated = current[:index] + [command] + current[index + 1:]
                write_setup_commands(context, updated)
                current = updated
                notice = (f"✓ Updated {command.name}", "positive")
            except BootstrapError as exc:
                notice = (f"Could not save: {exc}", "danger")
            continue
        if choice == "x":
            _show_manage_form(
                "Delete setup command",
                "Choose one saved command. The JSON file is updated atomically.",
                palette,
                columns=terminal_size.columns,
                color_enabled=color_enabled,
            )
            _show_setup_choices(
                current,
                palette,
                columns=terminal_size.columns,
                color_enabled=color_enabled,
            )
            index = _prompt_setup_index(current, "Command number to delete: ")
            if index is None:
                continue
            command = current[index]
            if not _confirm_manage_action(
                f"Delete '{command.name}'? [y/N] ",
                single_key_mode=single_key_mode,
            ):
                notice = ("Nothing changed.", "subtle")
                continue
            updated = current[:index] + current[index + 1:]
            write_setup_commands(context, updated)
            current = updated
            notice = (f"✓ Deleted {command.name}", "positive")
            continue
        if choice in ("u", "m"):
            _show_manage_form(
                "Reorder setup commands",
                "Commands run from top to bottom and stop at the first failure.",
                palette,
                columns=terminal_size.columns,
                color_enabled=color_enabled,
            )
            _show_setup_choices(
                current,
                palette,
                columns=terminal_size.columns,
                color_enabled=color_enabled,
            )
            index = _prompt_setup_index(current, "Command number to move: ")
            if index is None:
                continue
            destination = index - 1 if choice == "u" else index + 1
            if destination < 0 or destination >= len(current):
                notice = ("Command is already at that edge.", "warning")
                continue
            updated = current[:]
            updated[index], updated[destination] = updated[destination], updated[index]
            write_setup_commands(context, updated)
            current = updated
            notice = ("✓ Saved the new command order", "positive")
            continue
        if choice == "r":
            try:
                result = execute_action("setup", context, state_dir)
                count = result.get("setup_completed_count", 0)
                notice = (f"✓ Completed {count} setup command(s)", "positive")
            except (BootstrapError, NothingToDo) as exc:
                notice = (str(exc), "warning")
            continue
        if choice == "z":
            if not (context.source / SETUP_FILE).exists():
                notice = ("Setup configuration does not exist.", "warning")
                continue
            if not _confirm_manage_action(
                "Remove the setup configuration? [y/N] ",
                single_key_mode=single_key_mode,
            ):
                notice = ("Nothing changed.", "subtle")
                continue
            remove_setup_configuration(context)
            current = []
            notice = ("✓ Removed .herdr/worktree-setup.json", "positive")
            continue
        notice = ("Unknown key. Choose one of the actions below.", "warning")


def _direct_root_items(context: RepositoryContext, included: Sequence[str]) -> List[Tuple[str, str]]:
    included_set = set(included)
    rows: List[Tuple[str, str]] = []
    try:
        entries = sorted(os.scandir(context.source), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise BootstrapError(f"cannot list source root: {exc}") from exc
    for item in entries:
        name = item.name
        if name in (".git", ".herdr"):
            continue
        if name in included_set:
            label = "included"
        elif _git_has_tracked_path(context.source, name):
            label = "cannot add/tracked"
        elif _git_path_is_ignored(context.source, name):
            label = "can add/ignored"
        else:
            label = "cannot add/unignored"
        rows.append((name, label))
    return rows


def list_ignored_path_candidates(
    context: RepositoryContext,
    included: Sequence[str],
) -> List[str]:
    """List ignored paths without expanding whole ignored directory trees."""

    result = _run_git(
        context.source,
        [
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "--no-empty-directory",
            "-z",
        ],
        text=False,
    )
    candidates: List[str] = []
    seen = set()
    for encoded in result.stdout.split(b"\0"):
        if not encoded:
            continue
        candidate = os.fsdecode(encoded).rstrip("/")
        if not candidate or candidate == ".herdr" or candidate.startswith(".herdr/"):
            continue
        try:
            candidate = validate_relative_path(candidate)
            validate_copy_paths([*included, candidate])
            _ensure_no_symlink_parents(context.source, candidate)
        except BootstrapError:
            continue
        if candidate in seen or not _path_lexists(context.source / candidate):
            continue
        seen.add(candidate)
        candidates.append(candidate)
    return sorted(candidates, key=lambda value: (value.casefold(), value))


def _fzf_color(color: ColorValue) -> Optional[str]:
    if not isinstance(color, tuple):
        return None
    return "#{:02x}{:02x}{:02x}".format(*color)


def _fzf_color_spec(palette: ThemePalette) -> Optional[str]:
    values = {
        "fg": _fzf_color(palette.text),
        "fg+": _fzf_color(palette.text),
        "hl": _fzf_color(palette.accent),
        "hl+": _fzf_color(palette.accent),
        "pointer": _fzf_color(palette.accent),
        "marker": _fzf_color(palette.positive),
        "prompt": _fzf_color(palette.accent),
        "info": _fzf_color(palette.muted),
        "header": _fzf_color(palette.muted),
        "spinner": _fzf_color(palette.accent),
    }
    configured = [f"{name}:{value}" for name, value in values.items() if value]
    return ",".join(configured) if configured else None


def _fzf_executable(env: Mapping[str, str]) -> Optional[str]:
    return shutil.which("fzf", path=env.get("PATH"))


def select_paths_with_fzf(
    candidates: Sequence[str],
    palette: ThemePalette,
    *,
    operation: str,
    env: Mapping[str, str],
) -> Optional[List[str]]:
    """Return selected paths, or None when the picker is cancelled."""

    executable = _fzf_executable(env)
    if executable is None:
        raise BootstrapError("fzf is not available")
    if not candidates:
        return []
    if operation == "add":
        prompt = "Add files › "
        header = "Ignored paths available to copy into new worktrees"
    elif operation == "remove":
        prompt = "Stop copying › "
        header = "Registered paths · source files will not be deleted"
    else:
        raise BootstrapError(f"unknown path picker operation: {operation}")

    bindings = (
        "start:disable-search,"
        "j:down,k:up,space:toggle,"
        "/:enable-search+unbind(j,k,/)+change-prompt(Search › ),"
        "esc:abort"
    )
    command = [
        executable,
        "--read0",
        "--print0",
        "--multi",
        "--disabled",
        "--layout=reverse",
        "--style=minimal",
        "--border=none",
        "--header-border=none",
        "--input-border=none",
        "--list-border=none",
        "--footer-border=none",
        "--highlight-line",
        "--cycle",
        "--scheme=path",
        "--filepath-word",
        "--info=inline-right",
        "--pointer=›",
        "--marker=✓",
        "--scrollbar=│",
        f"--prompt={prompt}",
        f"--header={header}",
        "--header-first",
        "--footer=J/K or ↑/↓ move  ·  Space select  ·  Enter apply  ·  / search  ·  Esc cancel",
        f"--bind={bindings}",
    ]
    color_spec = _fzf_color_spec(palette)
    if color_spec:
        command.append(f"--color={color_spec}")
    child_env = dict(env)
    child_env["FZF_DEFAULT_OPTS"] = ""
    payload = b"\0".join(os.fsencode(path) for path in candidates) + b"\0"
    try:
        completed = subprocess.run(
            command,
            input=payload,
            stdout=subprocess.PIPE,
            check=False,
            env=child_env,
        )
    except OSError as exc:
        raise BootstrapError(f"could not open fzf: {exc}") from exc
    if completed.returncode == 0:
        selected = [os.fsdecode(value) for value in completed.stdout.split(b"\0") if value]
        selected_set = set(selected)
        return [path for path in candidates if path in selected_set]
    if completed.returncode in (1, 130):
        return None
    raise BootstrapError(f"fzf exited with code {completed.returncode}")


def _render_management_view(
    context: RepositoryContext,
    paths: Sequence[str],
    statuses: Sequence[CopyEntryStatus],
    commands: Sequence[SetupCommand],
    root_items: Sequence[Tuple[str, str]],
    palette: ThemePalette,
    *,
    columns: int,
    rows: int,
    decorated: bool,
    color_enabled: bool,
    details_visible: bool = False,
    scroll_offset: int = 0,
    notice: Optional[Tuple[str, str]] = None,
) -> ScrollableScreen:
    width = _ui_width(columns)
    addable = [name for name, label in root_items if label == "can add/ignored"]
    tracked = sum(label == "cannot add/tracked" for _name, label in root_items)
    unignored = sum(label == "cannot add/unignored" for _name, label in root_items)
    checkout_status = "PRIMARY CHECKOUT" if context.target_is_primary else "WORKTREE"
    checkout_role = "info" if context.target_is_primary else "positive"
    header = [
        "",
        _flat_line(
            [
                StyledSegment("◆ ", "accent", bold=True),
                StyledSegment("Worktree bootstrap", "text", bold=True),
                StyledSegment("  "),
                StyledSegment(checkout_status, checkout_role, bold=True),
            ],
            width,
            palette,
            color_enabled=color_enabled,
        ),
        _flat_line(
            [
                StyledSegment("Primary  ", "subtle"),
                StyledSegment(_friendly_path(context.source), "muted"),
            ],
            width,
            palette,
            color_enabled=color_enabled,
        ),
    ]
    if context.target != context.source:
        header.append(
            _flat_line(
                [
                    StyledSegment("Target   ", "subtle"),
                    StyledSegment(_friendly_path(context.target), "muted"),
                ],
                width,
                palette,
                color_enabled=color_enabled,
            )
        )

    body = [
        "",
        _section_heading(
            "Copy plan",
            f"{len(paths)} selected  ·  {len(addable)} root available",
            width,
            palette,
            color_enabled=color_enabled,
        ),
    ]
    if not paths:
        body.append(
            _flat_line(
                [StyledSegment("No local files selected.", "subtle", dim=True)],
                width,
                palette,
                color_enabled=color_enabled,
            )
        )
    else:
        for index, status_value in enumerate(statuses, 1):
            target_label = "target present" if status_value.target_exists else "new target"
            target_role = "positive" if status_value.target_exists else "info"
            body.append(
                _flat_line(
                    [
                        StyledSegment(f"{index:>2}  ", "subtle"),
                        StyledSegment(status_value.path, "text", bold=True),
                        StyledSegment("  ·  ", "subtle"),
                        StyledSegment(target_label, target_role),
                    ],
                    width,
                    palette,
                    color_enabled=color_enabled,
                )
            )
    if addable:
        preview = ", ".join(addable[:3])
        if len(addable) > 3:
            preview += f", +{len(addable) - 3}"
        body.append(
            _flat_line(
                [
                    StyledSegment("+ ", "positive", bold=True),
                    StyledSegment(preview, "muted"),
                ],
                width,
                palette,
                color_enabled=color_enabled,
            )
        )

    body.extend(
        [
            "",
            _section_heading(
                "Setup",
                f"{len(commands)} command{'s' if len(commands) != 1 else ''}",
                width,
                palette,
                color_enabled=color_enabled,
            ),
        ]
    )
    body.extend(
        _render_setup_rows(
            commands,
            width,
            palette,
            color_enabled=color_enabled,
            limit=max(1, len(commands)),
        )
    )

    if details_visible:
        body.extend(
            [
                "",
                _section_heading(
                    "Repository paths",
                    f"{tracked} tracked  ·  {unignored} unignored",
                    width,
                    palette,
                    color_enabled=color_enabled,
                ),
            ]
        )
        detail_rows = [item for item in root_items if item[1] in ("included", "can add/ignored")]
        if not detail_rows:
            body.append(
                _flat_line(
                    [StyledSegment("No eligible root paths.", "subtle", dim=True)],
                    width,
                    palette,
                    color_enabled=color_enabled,
                )
            )
        for name, label in detail_rows:
            included = label == "included"
            body.append(
                _flat_line(
                    [
                        StyledSegment("✓ " if included else "+ ", "accent" if included else "positive", bold=True),
                        StyledSegment(name, "text"),
                        StyledSegment("  included" if included else "  ignored · available", "muted", dim=True),
                    ],
                    width,
                    palette,
                    color_enabled=color_enabled,
                )
            )
    actions: List[ActionSpec] = [
        ("A", "Add files", "accent"),
        ("D", "Stop copying", "danger"),
        ("I", "Type path", "accent"),
        ("C", "Setup", "accent"),
        ("S", "Sync", "positive"),
        ("T", "Status", "info"),
        ("V", "Hide paths" if details_visible else "Paths", "accent"),
        ("Q", "Close", "accent"),
    ]
    return _compose_scrollable_screen(
        header,
        body,
        actions,
        width,
        rows,
        palette,
        color_enabled=color_enabled,
        requested_offset=scroll_offset,
        notice=notice,
    )


def render_management_screen(
    context: RepositoryContext,
    paths: Sequence[str],
    statuses: Sequence[CopyEntryStatus],
    commands: Sequence[SetupCommand],
    root_items: Sequence[Tuple[str, str]],
    palette: ThemePalette,
    *,
    columns: int,
    rows: int,
    decorated: bool,
    color_enabled: bool,
    details_visible: bool = False,
    scroll_offset: int = 0,
    notice: Optional[Tuple[str, str]] = None,
) -> List[str]:
    return list(
        _render_management_view(
            context,
            paths,
            statuses,
            commands,
            root_items,
            palette,
            columns=columns,
            rows=rows,
            decorated=decorated,
            color_enabled=color_enabled,
            details_visible=details_visible,
            scroll_offset=scroll_offset,
            notice=notice,
        ).lines
    )


def render_status_screen(
    status: Sequence[str],
    palette: ThemePalette,
    *,
    columns: int,
    color_enabled: bool,
) -> List[str]:
    width = _ui_width(columns)
    lines = [
        "",
        _flat_line(
            [
                StyledSegment("◆ ", "accent", bold=True),
                StyledSegment("Worktree bootstrap", "text", bold=True),
                StyledSegment("  /  Status", "muted"),
            ],
            width,
            palette,
            color_enabled=color_enabled,
        ),
        "",
    ]
    for value in status:
        if ":" in value:
            label, detail = value.split(":", 1)
            lines.append(
                _flat_line(
                    [
                        StyledSegment(f"{label:<16}", "subtle"),
                        StyledSegment(detail.strip(), "text"),
                    ],
                    width,
                    palette,
                    color_enabled=color_enabled,
                )
            )
        else:
            lines.append(
                _flat_line(
                    [StyledSegment(value, "text")],
                    width,
                    palette,
                    color_enabled=color_enabled,
                )
            )
    lines.extend(
        [
            "",
            _flat_separator(width, palette, color_enabled=color_enabled),
            _flat_line(
                [StyledSegment("ANY KEY", "accent", bold=True), StyledSegment(" Back")],
                width,
                palette,
                color_enabled=color_enabled,
            ),
            "",
        ]
    )
    return lines


def _clear_screen() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def manage(
    context: RepositoryContext,
    state_dir: Path,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    effective_env = os.environ if env is None else env
    palette, decorated, color_enabled = _ui_appearance(effective_env)
    single_key_mode = decorated and sys.stdin.isatty()
    details_visible = False
    scroll_offset = 0
    notice: Optional[Tuple[str, str]] = None
    renderer = TerminalScreenRenderer(decorated=decorated)
    if (context.source / SETUP_FILE).exists():
        ensure_control_excluded(context, SETUP_FILE)
    while True:
        terminal_size = shutil.get_terminal_size((80, 24))
        try:
            paths = read_copy_list(context.source)
            root_items = _direct_root_items(context, paths)
            commands = load_setup_commands(context.source)
            statuses = classify_copy_entries(context.source, context.target, paths)
        except BootstrapError as exc:
            print(f"Configuration error: {exc}")
            print("Edit the affected control file, then press Enter to refresh or q to quit.")
            choice = normalize_manage_choice(input("> "))
            if choice == "q":
                return 1
            continue

        screen = _render_management_view(
            context,
            paths,
            statuses,
            commands,
            root_items,
            palette,
            columns=terminal_size.columns,
            rows=terminal_size.lines,
            decorated=decorated,
            color_enabled=color_enabled,
            details_visible=details_visible,
            scroll_offset=scroll_offset,
            notice=notice,
        )
        scroll_offset = screen.offset
        renderer.draw(screen.lines)
        notice = None
        try:
            choice = _read_manage_choice(single_key_mode=single_key_mode)
        except EOFError:
            return 0
        if choice == "q":
            return 0
        if choice in ("", "r"):
            continue
        if choice == "k":
            scroll_offset = max(0, scroll_offset - 1)
            continue
        if choice == "j":
            scroll_offset = min(screen.maximum_offset, scroll_offset + 1)
            continue
        if choice == "page-up":
            scroll_offset = max(0, scroll_offset - screen.page_size)
            continue
        if choice == "page-down":
            scroll_offset = min(screen.maximum_offset, scroll_offset + screen.page_size)
            continue
        if choice == "v":
            details_visible = not details_visible
            scroll_offset = 10**9 if details_visible else 0
            continue
        if choice == "a" and _fzf_executable(effective_env):
            try:
                candidates = list_ignored_path_candidates(context, paths)
                if not candidates:
                    notice = ("No additional ignored paths are available.", "warning")
                    continue
                selected = select_paths_with_fzf(
                    candidates,
                    palette,
                    operation="add",
                    env=effective_env,
                )
                if selected is None:
                    notice = ("Nothing changed.", "subtle")
                    continue
                if not selected:
                    notice = ("No paths were selected.", "warning")
                    continue
                updated = validate_copy_paths([*paths, *selected])
                write_copy_list(context, updated)
                notice = (
                    f"✓ Added {len(selected)} path{'s' if len(selected) != 1 else ''} to the copy plan",
                    "positive",
                )
            except BootstrapError as exc:
                notice = (f"Could not add paths: {exc}", "danger")
            continue
        if choice in ("a", "i"):
            _show_manage_form(
                "Add a local path",
                "Enter one repository-relative ignored path from the primary checkout.",
                palette,
                columns=terminal_size.columns,
                color_enabled=color_enabled,
            )
            candidate = input("Repository-relative path: ").strip()
            try:
                candidate = validate_relative_path(candidate)
                validate_copy_paths([*paths, candidate])
                if not _path_lexists(context.source / candidate):
                    raise BootstrapError("source path does not exist")
                _ensure_no_symlink_parents(context.source, candidate)
                status_value = classify_copy_entry(context.source, context.target, candidate)
                if status_value.eligibility != "ignored":
                    raise BootstrapError(status_value.detail)
                write_copy_list(context, [*paths, candidate])
                notice = (f"✓ Added {candidate} to the copy plan", "positive")
            except BootstrapError as exc:
                notice = (f"Could not add path: {exc}", "danger")
            continue
        if choice == "d":
            if not paths:
                notice = ("The copy plan is already empty.", "warning")
                continue
            if _fzf_executable(effective_env):
                try:
                    selected = select_paths_with_fzf(
                        paths,
                        palette,
                        operation="remove",
                        env=effective_env,
                    )
                    if selected is None:
                        notice = ("Nothing changed.", "subtle")
                        continue
                    if not selected:
                        notice = ("No paths were selected.", "warning")
                        continue
                    selected_set = set(selected)
                    updated = [path for path in paths if path not in selected_set]
                    write_copy_list(context, updated)
                    notice = (
                        f"✓ Stopped copying {len(selected)} path{'s' if len(selected) != 1 else ''}",
                        "positive",
                    )
                except BootstrapError as exc:
                    notice = (f"Could not update paths: {exc}", "danger")
                continue
            _show_manage_form(
                "Remove a local path",
                "Choose an entry from the copy plan. Source files are never deleted.",
                palette,
                columns=terminal_size.columns,
                color_enabled=color_enabled,
            )
            for index, path in enumerate(paths, 1):
                print(f"  {index:>2}. {path}")
            print()
            raw_index = input("Entry number to delete: ").strip()
            try:
                index = int(raw_index) - 1
                if index < 0 or index >= len(paths):
                    raise ValueError
            except ValueError:
                notice = ("Invalid copy-plan entry number.", "warning")
                continue
            removed = paths[index]
            write_copy_list(context, paths[:index] + paths[index + 1 :])
            notice = (f"✓ Removed {removed} from the copy plan", "positive")
            continue
        if choice == "c":
            manage_setup_commands(context, state_dir, commands, env=effective_env)
            continue
        if choice == "s":
            try:
                result = execute_action("sync", context, state_dir)
                count = result.get("copied_count", 0)
                notice = (f"✓ Synced {count} item(s)", "positive")
            except (BootstrapError, NothingToDo) as exc:
                notice = (str(exc), "warning")
            continue
        if choice == "t":
            lines, _ = status_lines(context, state_dir)
            _clear_screen()
            for line in render_status_screen(
                lines,
                palette,
                columns=terminal_size.columns,
                color_enabled=color_enabled,
            ):
                print(line)
            try:
                _read_manage_choice(single_key_mode=single_key_mode, prompt="Press Enter to return: ")
            except EOFError:
                return 0
            continue
        notice = ("Unknown key. Choose one of the actions below.", "warning")


def _find_workspace_id_for_path(value: Any, target: Path) -> Optional[str]:
    if isinstance(value, list):
        for item in value:
            found = _find_workspace_id_for_path(item, target)
            if found:
                return found
        return None
    if not isinstance(value, dict):
        return None
    raw_worktree = value.get("worktree")
    candidates: List[Any] = [value.get("cwd"), value.get("path")]
    if isinstance(raw_worktree, dict):
        candidates.extend(
            [
                raw_worktree.get("checkout_path"),
                raw_worktree.get("path"),
            ]
        )
    if any(
        isinstance(candidate, str) and _absolute_path(Path(candidate)) == target
        for candidate in candidates
    ):
        workspace_id = value.get("workspace_id") or value.get("id")
        if isinstance(workspace_id, str) and workspace_id:
            return workspace_id
    for nested in value.values():
        found = _find_workspace_id_for_path(nested, target)
        if found:
            return found
    return None


def query_herdr_workspace_id(
    target: Path,
    env: Mapping[str, str],
    *,
    fallback: Optional[str] = None,
) -> Optional[str]:
    herdr = _herdr_command(env)
    if not herdr:
        return fallback
    try:
        result = subprocess.run(
            [herdr, "workspace", "list"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return fallback
    if result.returncode != 0:
        return fallback
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError:
        return fallback
    return _find_workspace_id_for_path(document, _absolute_path(target)) or fallback


def _worktree_dirty_count(path: Path) -> int:
    result = _run_git(
        path,
        ["status", "--porcelain", "-z", "--untracked-files=all"],
        check=False,
        text=False,
    )
    if result.returncode != 0:
        raise BootstrapError("could not inspect worktree changes before removal")
    return len([entry for entry in result.stdout.split(b"\0") if entry])


def remove_managed_worktree(
    item: WorktreeInventoryItem,
    state_dir: Path,
    env: Mapping[str, str],
    *,
    force: bool = False,
) -> Mapping[str, Any]:
    if item.status != "active" or not item.registered:
        raise BootstrapError("only an active registered worktree can be removed")
    if item.worktree_path == item.repo_root:
        raise BootstrapError("the primary checkout cannot be removed")
    if not item.provenance_verified:
        raise BootstrapError("refusing to remove a worktree without recorded provenance")
    dirty_count = _worktree_dirty_count(item.worktree_path)
    if dirty_count and not force:
        raise BootstrapError(
            f"worktree has {dirty_count} modified or untracked entries; force confirmation is required"
        )
    workspace_id = query_herdr_workspace_id(
        item.worktree_path,
        env,
        fallback=item.workspace_id,
    )
    if not workspace_id:
        raise BootstrapError("cannot identify the active Herdr workspace for this worktree")
    commands = load_lifecycle_commands(item.repo_root)["before_remove"]
    operation_id = uuid.uuid4().hex
    started_at = dt.datetime.now(dt.timezone.utc).isoformat()
    if load_worktree_mapping(state_dir, item.worktree_path) is None:
        _atomic_write_json(
            worktree_map_path(state_dir, item.worktree_path),
            {
                "version": 2,
                "target": os.fspath(item.worktree_path),
                "repo_root": os.fspath(item.repo_root),
                "common_git_dir": os.fspath(_git_common_dir(item.repo_root)),
                "branch": item.branch,
                "created_head_oid": item.head_oid,
                "base_candidates": list(item.base_candidates),
                "workspace_id": item.workspace_id,
                "recorded_at": started_at,
                "status": "active",
                "provenance_verified": True,
            },
        )
    _update_worktree_mapping(
        state_dir,
        item.worktree_path,
        {
            "removal_operation_id": operation_id,
            "removal_operation_status": "running",
            "removal_requested_at": started_at,
            "removal_requested_force": force,
            "before_remove_status": "running",
        },
    )
    try:
        completed_count = run_lifecycle_commands(
            item.worktree_path,
            commands,
            phase="before_remove",
            environment=_lifecycle_environment(
                worktree_path=item.worktree_path,
                repo_root=item.repo_root,
                branch=item.branch,
                head_oid=item.head_oid or item.created_head_oid,
                forced=force,
                leftover=False,
            ),
        )
    except BootstrapError as exc:
        _update_worktree_mapping(
            state_dir,
            item.worktree_path,
            {
                "removal_operation_status": "blocked",
                "before_remove_status": "failed",
                "before_remove_error": str(exc),
                "before_remove_finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        )
        raise BootstrapError(f"before-remove lifecycle blocked removal: {exc}") from exc
    _update_worktree_mapping(
        state_dir,
        item.worktree_path,
        {
            "before_remove_status": "success",
            "before_remove_completed_count": completed_count,
            "before_remove_finished_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
    )
    final_dirty_count = _worktree_dirty_count(item.worktree_path)
    if final_dirty_count and not force:
        message = (
            f"worktree has {final_dirty_count} modified or untracked entries after "
            "before-remove lifecycle; review and retry with force confirmation"
        )
        _update_worktree_mapping(
            state_dir,
            item.worktree_path,
            {
                "removal_operation_status": "blocked",
                "removal_error": message,
            },
        )
        raise BootstrapError(message)
    herdr = _herdr_command(env)
    if not herdr:
        _update_worktree_mapping(
            state_dir,
            item.worktree_path,
            {
                "removal_operation_status": "failed",
                "removal_error": "herdr is required for managed worktree removal",
            },
        )
        raise BootstrapError("herdr is required for managed worktree removal")
    command = [herdr, "worktree", "remove", "--workspace", workspace_id]
    if force:
        command.append("--force")
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _update_worktree_mapping(
            state_dir,
            item.worktree_path,
            {"removal_operation_status": "failed", "removal_error": str(exc)},
        )
        raise BootstrapError("Herdr worktree removal did not complete") from exc
    if result.returncode != 0:
        error = result.stderr.strip() or result.stdout.strip() or "Herdr refused worktree removal"
        _update_worktree_mapping(
            state_dir,
            item.worktree_path,
            {"removal_operation_status": "failed", "removal_error": error},
        )
        raise BootstrapError(error)
    return _update_worktree_mapping(
        state_dir,
        item.worktree_path,
        {"removal_operation_status": "requested"},
    )


def _lifecycle_phase_title(phase: str) -> str:
    return {
        "on_created": "After worktree creation",
        "before_remove": "Before managed removal",
        "after_remove": "After worktree removal",
    }[phase]


def manage_lifecycle_configuration(
    context: RepositoryContext,
    state_dir: Path,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    del state_dir
    while True:
        hooks = {phase: list(commands) for phase, commands in load_lifecycle_commands(context.source).items()}
        output_fn("Worktree lifecycle commands")
        for index, phase in enumerate(LIFECYCLE_PHASES, 1):
            output_fn(f"  {index}. {_lifecycle_phase_title(phase)}: {len(hooks[phase])} command(s)")
        choice = input_fn("Phase [1-3], or Q to return: ").strip().casefold()
        if choice in ("q", "quit", "escape"):
            return 0
        try:
            phase = LIFECYCLE_PHASES[int(choice) - 1]
        except (ValueError, IndexError):
            output_fn("Invalid lifecycle phase.")
            continue
        while True:
            commands = hooks[phase]
            output_fn(_lifecycle_phase_title(phase))
            for index, command in enumerate(commands, 1):
                output_fn(
                    f"  {index}. {command.name}: {shlex.join(command.argv)} "
                    f"({command.timeout_seconds}s)"
                )
            action = input_fn("[N]ew  [E]dit  [X]delete  [U]p  [M]down  [Q]back: ").strip().casefold()
            if action in ("q", "quit", "escape"):
                break
            if action in ("n", "new"):
                command = _prompt_setup_command(input_fn=input_fn)
                hooks[phase] = [*commands, command]
            elif action in ("e", "edit", "x", "delete", "u", "up", "m", "down"):
                raw_index = input_fn("Command number: ").strip()
                try:
                    index = int(raw_index) - 1
                    if index < 0 or index >= len(commands):
                        raise ValueError
                except ValueError:
                    output_fn("Invalid command number.")
                    continue
                if action in ("e", "edit"):
                    updated_command = _prompt_setup_command(commands[index], input_fn=input_fn)
                    hooks[phase] = commands[:index] + [updated_command] + commands[index + 1:]
                elif action in ("x", "delete"):
                    hooks[phase] = commands[:index] + commands[index + 1:]
                else:
                    destination = index - 1 if action in ("u", "up") else index + 1
                    if destination < 0 or destination >= len(commands):
                        output_fn("Command is already at that edge.")
                        continue
                    updated_commands = commands[:]
                    updated_commands[index], updated_commands[destination] = (
                        updated_commands[destination],
                        updated_commands[index],
                    )
                    hooks[phase] = updated_commands
            else:
                output_fn("Unknown action.")
                continue
            write_lifecycle_commands(context, hooks)
            output_fn("Lifecycle configuration saved.")


def _choose_inventory_item(
    candidates: Sequence[WorktreeInventoryItem],
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> Optional[WorktreeInventoryItem]:
    if not candidates:
        return None
    for index, item in enumerate(candidates, 1):
        output_fn(f"  {index}. [{item.status}] {item.branch or '(detached/unknown)'}")
        output_fn(f"     {item.worktree_path}")
    raw = input_fn("Worktree number: ").strip()
    try:
        index = int(raw) - 1
    except ValueError:
        return None
    return candidates[index] if 0 <= index < len(candidates) else None


def manage_worktree_inventory(
    context: RepositoryContext,
    state_dir: Path,
    *,
    env: Optional[Mapping[str, str]] = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    effective_env = os.environ if env is None else env
    while True:
        if output_fn is print:
            _clear_screen()
        worktree_root = load_worktree_directory(effective_env)
        items = collect_worktree_inventory(
            context.source,
            state_dir,
            worktree_root=worktree_root,
        )
        for line in inventory_lines(
            context.source,
            state_dir,
            env=effective_env,
            items=items,
        ):
            output_fn(line)
        output_fn("")
        choice = input_fn(
            "[R]escan  [L]ifecycle  [D]elete worktree  [C]lean residue  [B]ranches  [Q]uit: "
        ).strip().casefold()
        if choice in ("q", "quit", "escape"):
            return 0
        if choice in ("", "r", "rescan"):
            continue
        if choice in ("l", "lifecycle"):
            manage_lifecycle_configuration(context, state_dir, input_fn=input_fn, output_fn=output_fn)
            continue
        if choice in ("b", "branches"):
            review_pending_cleanups(
                state_dir,
                input_fn=input_fn,
                output_fn=output_fn,
                env=effective_env,
            )
            continue
        if choice in ("d", "delete"):
            candidates = [item for item in items if item.status == "active" and item.worktree_path != item.repo_root]
            selected = _choose_inventory_item(candidates, input_fn, output_fn)
            if selected is None:
                output_fn("No valid active worktree was selected.")
                continue
            dirty_count = _worktree_dirty_count(selected.worktree_path)
            token = selected.branch or selected.worktree_path.name
            if dirty_count:
                output_fn(f"Warning: {dirty_count} modified or untracked entries require forced removal.")
            confirmation = input_fn(f"Type '{token}' to remove this worktree: ").strip()
            if confirmation != token:
                output_fn("Confirmation did not match; nothing was removed.")
                continue
            try:
                remove_managed_worktree(
                    selected,
                    state_dir,
                    effective_env,
                    force=dirty_count > 0,
                )
                output_fn("Herdr worktree removal requested.")
            except BootstrapError as exc:
                output_fn(f"Removal failed: {exc}")
            continue
        if choice in ("c", "clean"):
            candidates = [
                item
                for item in items
                if item.status == "residue" and item.provenance_verified
            ]
            selected = _choose_inventory_item(candidates, input_fn, output_fn)
            if selected is None:
                output_fn("No valid leftover directory was selected.")
                continue
            confirmation = input_fn(
                f"Type '{selected.worktree_path.name}' to remove leftover files: "
            ).strip()
            if confirmation != selected.worktree_path.name:
                output_fn("Confirmation did not match; nothing was removed.")
                continue
            try:
                remove_leftover_worktree_files(
                    selected.repo_root,
                    selected.worktree_path,
                    worktree_root=worktree_root,
                    provenance_verified=selected.provenance_verified,
                    expected_device=selected.leftover.device,
                    expected_inode=selected.leftover.inode,
                )
                _update_worktree_mapping(
                    state_dir,
                    selected.worktree_path,
                    {
                        "checkout_cleanup_status": "removed",
                        "checkout_cleanup_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    },
                )
                output_fn("Leftover worktree files removed.")
            except BootstrapError as exc:
                output_fn(f"Cleanup failed: {exc}")
            continue
        output_fn("Unknown inventory action.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap Herdr Git worktrees")
    parser.add_argument(
        "action",
        choices=(
            "bootstrap",
            "sync",
            "setup",
            "status",
            "manage-launch",
            "manage",
            "setup-manage-launch",
            "setup-manage",
            "inventory-launch",
            "inventory",
            "branch-cleanup-event",
            "branch-cleanup",
        ),
    )
    parser.add_argument("--target", help="explicit target checkout (primarily for testing/manual use)")
    parser.add_argument("--state-dir", help="override plugin state directory")
    return parser


def main(argv: Optional[Sequence[str]] = None, env: Optional[Mapping[str, str]] = None) -> int:
    args = build_parser().parse_args(argv)
    effective_env = os.environ if env is None else env
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir(effective_env)
    try:
        if args.action == "branch-cleanup-event":
            handle_branch_cleanup_event(effective_env, state_dir)
            return 0
        if args.action == "branch-cleanup":
            cleanup_id = effective_env.get("HERDR_BRANCH_CLEANUP_ID")
            return review_pending_cleanups(state_dir, cleanup_id=cleanup_id, env=effective_env)
        target_path = resolve_target_path(args.target, effective_env)
        if args.action == "manage-launch":
            return launch_management_ui("manage", target_path, effective_env)
        if args.action == "setup-manage-launch":
            return launch_management_ui("setup-manage", target_path, effective_env)
        if args.action == "inventory-launch":
            return launch_management_ui("inventory", target_path, effective_env)
        context = resolve_repository(target_path)
        if args.action == "status":
            lines, invalid = status_lines(context, state_dir)
            print("\n".join(lines))
            return 2 if invalid else 0
        if args.action == "manage":
            return manage(context, state_dir, env=effective_env)
        if args.action == "setup-manage":
            if (context.source / SETUP_FILE).exists():
                ensure_control_excluded(context, SETUP_FILE)
            return manage_setup_commands(
                context,
                state_dir,
                load_setup_commands(context.source),
                env=effective_env,
            )
        if args.action == "inventory":
            return manage_worktree_inventory(
                context,
                state_dir,
                env=effective_env,
            )
        if args.action == "bootstrap":
            record_worktree_mapping(context, state_dir, env=effective_env)
        execute_action(args.action, context, state_dir)
        return 0
    except NothingToDo as exc:
        if args.action in ("manage", "setup-manage", "inventory"):
            notify_repository_unavailable(exc, effective_env)
        print(str(exc))
        return 0
    except KeyboardInterrupt:
        print("bootstrap interrupted", file=sys.stderr)
        return 130
    except BootstrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
