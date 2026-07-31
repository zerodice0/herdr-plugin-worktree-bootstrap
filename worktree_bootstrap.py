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
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import uuid


PLUGIN_ID = "zerodice0.worktree-bootstrap"
COPY_LIST = Path(".herdr/worktree-copy.list")
SETUP_FILE = Path(".herdr/worktree-setup.json")
TXN_PREFIX = ".herdr-worktree-bootstrap-txn-"


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


def parse_worktree_porcelain(raw: bytes) -> List[Path]:
    """Extract worktree paths from `git worktree list --porcelain -z`."""

    paths: List[Path] = []
    for field in raw.split(b"\0"):
        if field.startswith(b"worktree "):
            value = field[len(b"worktree ") :]
            paths.append(Path(os.fsdecode(value)))
    return paths


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
    if _is_bare(target_path):
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


def load_setup_commands(source: Path) -> List[SetupCommand]:
    path = source / SETUP_FILE
    if not path.exists():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"{SETUP_FILE} is invalid JSON at line {exc.lineno}: {exc.msg}") from exc
    except (OSError, UnicodeError) as exc:
        raise BootstrapError(f"cannot read {SETUP_FILE}: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"version", "commands"}:
        raise BootstrapError(f"{SETUP_FILE} must contain only 'version' and 'commands'")
    if document["version"] != 1 or isinstance(document["version"], bool):
        raise BootstrapError(f"{SETUP_FILE} version must be 1")
    if not isinstance(document["commands"], list):
        raise BootstrapError(f"{SETUP_FILE} commands must be an array")

    commands: List[SetupCommand] = []
    allowed = {"name", "argv", "timeout_seconds"}
    for index, raw in enumerate(document["commands"]):
        label = f"{SETUP_FILE} command {index + 1}"
        if not isinstance(raw, dict) or set(raw) != allowed:
            raise BootstrapError(f"{label} must contain only name, argv, and timeout_seconds")
        name = raw["name"]
        argv = raw["argv"]
        timeout = raw["timeout_seconds"]
        if not isinstance(name, str) or not name.strip():
            raise BootstrapError(f"{label} name must be a non-empty string")
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            raise BootstrapError(f"{label} argv must be a non-empty array of strings")
        if not argv[0]:
            raise BootstrapError(f"{label} executable cannot be empty")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise BootstrapError(f"{label} timeout_seconds must be a positive integer")
        commands.append(SetupCommand(name=name, argv=tuple(argv), timeout_seconds=timeout))
    return commands


def _terminate_process_group(process: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def run_setup_commands(target: Path, commands: Sequence[SetupCommand]) -> int:
    completed_count = 0
    for index, command in enumerate(commands, 1):
        print(f"setup {index}/{len(commands)}: {command.name}", flush=True)
        try:
            process = subprocess.Popen(
                list(command.argv),
                cwd=os.fspath(target),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise SetupCommandError(
                f"setup command {index} ({command.name}) executable not found: {command.argv[0]}",
                command_index=index,
                command_name=command.name,
                failure_kind="executable_not_found",
            ) from exc
        except OSError as exc:
            raise SetupCommandError(
                f"setup command {index} ({command.name}) could not start: {exc}",
                command_index=index,
                command_name=command.name,
                failure_kind="start_failed",
            ) from exc
        try:
            return_code = process.wait(timeout=command.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process)
            raise SetupCommandError(
                f"setup command {index} ({command.name}) timed out after {command.timeout_seconds}s",
                command_index=index,
                command_name=command.name,
                failure_kind="timeout",
            ) from exc
        except KeyboardInterrupt:
            _terminate_process_group(process)
            raise
        if return_code != 0:
            raise SetupCommandError(
                f"setup command {index} ({command.name}) failed with exit code {return_code}",
                command_index=index,
                command_name=command.name,
                failure_kind="nonzero_exit",
                command_exit_code=return_code,
            )
        completed_count += 1
    return completed_count


def default_state_dir(env: Mapping[str, str]) -> Path:
    if env.get("HERDR_PLUGIN_STATE_DIR"):
        return Path(env["HERDR_PLUGIN_STATE_DIR"]).expanduser()
    if env.get("XDG_STATE_HOME"):
        return Path(env["XDG_STATE_HOME"]).expanduser() / "herdr" / "plugins" / PLUGIN_ID
    return Path.home() / ".local" / "state" / "herdr" / "plugins" / PLUGIN_ID


def target_key(context: RepositoryContext) -> str:
    value = f"{context.common_git_dir}\0{context.target}".encode("utf-8", "surrogateescape")
    return hashlib.sha256(value).hexdigest()[:24]


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
                result["setup_completed_count"] = run_setup_commands(context.target, commands)
                print(f"completed {result['setup_completed_count']} setup command(s)", flush=True)
            result["status"] = "success"
            write_run_state(state_dir, context, result)
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
        commands = load_setup_commands(context.source)
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


def _clear_screen() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def manage(context: RepositoryContext, state_dir: Path) -> int:
    if (context.source / SETUP_FILE).exists():
        ensure_control_excluded(context, SETUP_FILE)
    while True:
        _clear_screen()
        try:
            paths = read_copy_list(context.source)
            root_items = _direct_root_items(context, paths)
            commands = load_setup_commands(context.source)
        except BootstrapError as exc:
            print(f"Configuration error: {exc}")
            print("Edit the affected control file, then press Enter to refresh or q to quit.")
            choice = input("> ").strip().lower()
            if choice == "q":
                return 1
            continue

        print("Herdr Worktree Bootstrap")
        print(f"Primary checkout: {context.source}")
        print(f"Current target:   {context.target}")
        print("\nCopy list:")
        if paths:
            for index, path in enumerate(paths, 1):
                print(f"  {index:>2}. {path}")
        else:
            print("  (empty)")
        print("\nRepository root (not recursively scanned):")
        for name, label in root_items:
            print(f"  [{label}] {name}")
        print("\nSetup commands (read-only; edit .herdr/worktree-setup.json):")
        if commands:
            for command in commands:
                print(f"  {command.name}: {list(command.argv)!r} ({command.timeout_seconds}s)")
        else:
            print("  (none)")
        print("\n[a] add path  [d] delete path  [s] sync now  [t] status  [r] refresh  [q] quit")
        choice = input("> ").strip().lower()
        if choice == "q":
            return 0
        if choice in ("", "r"):
            continue
        if choice == "a":
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
                print(f"Added {candidate}. Press Enter.")
            except BootstrapError as exc:
                print(f"Not added: {exc}. Press Enter.")
            input()
            continue
        if choice == "d":
            raw_index = input("Entry number to delete: ").strip()
            try:
                index = int(raw_index) - 1
                if index < 0 or index >= len(paths):
                    raise ValueError
            except ValueError:
                print("Invalid entry number. Press Enter.")
                input()
                continue
            removed = paths[index]
            write_copy_list(context, paths[:index] + paths[index + 1 :])
            print(f"Removed {removed}. Press Enter.")
            input()
            continue
        if choice == "s":
            try:
                execute_action("sync", context, state_dir)
            except (BootstrapError, NothingToDo) as exc:
                print(exc)
            print("Press Enter.")
            input()
            continue
        if choice == "t":
            lines, _ = status_lines(context, state_dir)
            print("\n" + "\n".join(lines))
            print("\nPress Enter.")
            input()
            continue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap Herdr Git worktrees")
    parser.add_argument("action", choices=("bootstrap", "sync", "setup", "status", "manage"))
    parser.add_argument("--target", help="explicit target checkout (primarily for testing/manual use)")
    parser.add_argument("--state-dir", help="override plugin state directory")
    return parser


def main(argv: Optional[Sequence[str]] = None, env: Optional[Mapping[str, str]] = None) -> int:
    args = build_parser().parse_args(argv)
    effective_env = os.environ if env is None else env
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir(effective_env)
    try:
        target_path = resolve_target_path(args.target, effective_env)
        context = resolve_repository(target_path)
        if args.action == "status":
            lines, invalid = status_lines(context, state_dir)
            print("\n".join(lines))
            return 2 if invalid else 0
        if args.action == "manage":
            return manage(context, state_dir)
        execute_action(args.action, context, state_dir)
        return 0
    except NothingToDo as exc:
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
