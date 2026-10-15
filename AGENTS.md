# Agent instructions

## Working on this repository

- `worktree_bootstrap.py` is the single implementation module and direct CLI entrypoint.
- `tests/test_worktree_bootstrap.py` contains the unit and integration suite; `tests/herdr_environment_smoke.py` covers Herdr action/event environments.
- `herdr-plugin.toml` declares the plugin actions and lifecycle events. `remove-worktree` is CLI-only, not a manifest action.
- Use Python 3.9+ and the standard library only; do not add third-party dependencies. CI tests Python 3.9–3.14 on Ubuntu and macOS.
- README is the user-facing CLI and configuration contract. Update it when behavior changes, and put changes under CHANGELOG's `Unreleased` heading.
- Lifecycle commands are argv arrays, executed without a shell. Do not introduce shell execution or interpolation.
- Never log argv values, environment values, file contents, or secrets. Status already intentionally exposes configured argv; do not expand that exception to logs. Child commands are responsible for their own output.

Run the standard checks when the assignment calls for verification:

```sh
python3 -m unittest discover -s tests -v
python3 tests/herdr_environment_smoke.py
```

## Commit messages

Every commit in this repository follows [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/) and is written in English.

- Header: `<type>[optional scope][!]: <description>`, at most 72 characters. Use the imperative mood, start the description in lowercase, and omit the trailing period.
- Types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`, `style`, `revert`. Pick the type from the user-visible effect; a change that adds a feature together with its tests and docs is `feat`.
- Scope is optional. When used, name the affected area, such as `cli`, `cleanup`, `inventory`, `lifecycle`, `copy`, `ui`, `manifest`, `docs`, or `ci`.
- Body: separate it from the header with a blank line, wrap at 72 columns, and explain what changed and why. Use short `-` bullets when listing several changes.
- Breaking changes: add `!` after the type or scope and a `BREAKING CHANGE: <description>` footer that states what consumers must change. CLI flags, JSON fields, error codes, settings, and control-file schemas count as public contract.
- Version bumps: use `chore(release): <version>` when a commit only bumps the version, or mention the bump in the body of the commit that includes it.
- Do not put argv values, environment values, file contents, or secrets in commit messages.

## Safety invariants

Keep headless and interactive paths subject to the same safeguards; never bypass them to make automation succeed:

- Never delete `main`, `master`, `develop`, `development`, `trunk`, the detected default branch, or a branch checked out in another worktree.
- Reinspect branch state immediately before deletion and compare the expected HEAD OID. Force deletion requires the exact branch name; headless safe deletion also requires exact branch confirmation. Never escalate safe deletion to force automatically.
- Keep leftover-file deletion separate from branch deletion. Require verified provenance, the configured root and canonical-parent checks, a real non-Git directory that is not registered as a worktree, fresh device/inode checks, and exact directory-name confirmation. Never delete unverified discovered paths.
- Run `before_remove` before requesting Herdr removal and recheck dirty state afterward. Fail closed on command errors or disallowed newly dirty state; `--force` must not bypass other checks.
- Preserve the no-shell and no-secret-logging rules above.

## Using the plugin headlessly

See [README: Headless and agent use](README.md#headless-and-agent-use) for exact flags, JSON fields, settings, error codes, and the end-to-end workflow.

- Find the installed plugin root with `herdr plugin list`; a linked plugin uses its checkout. Invoke `python3 <plugin root>/worktree_bootstrap.py <action> --target <path>` directly. `herdr plugin action invoke` always targets the focused workspace, not the caller's directory or workspace environment overrides.
- Inspect with `status --json`, `inventory --json`, and `branch-cleanup --list --json`. Set `branch_cleanup_popup` to `"never"` in Herdr's actual plugin `settings.json` before unattended removal; this retains queued cleanup and `after_remove` commands.
- Create with `herdr worktree create --cwd <repo> --branch <b> --base main --path <p> --no-focus`, wait for asynchronous bootstrap, then use `remove-worktree --target <p> --confirm <token> --json` (add `--force` only for intentionally forced dirty removal).
- Use the returned `branch_cleanup_id` with `branch-cleanup --id <id> --decision keep|delete|force`; deletion requires `--confirm <branch>`. Briefly retry `cleanup_not_found` while the asynchronous event queues the request. If files remain, separately choose `--checkout keep` or `--checkout remove --confirm-checkout <directory name>`. Read `resolved` and `needs`, not just the exit code.

## Safe experiments and smoke tests

- Never change the user's workspace focus or close anyone else's workspaces. Use `--no-focus` when creating experimental worktrees.
- Use throwaway repositories under `/tmp` and an isolated `--state-dir` or `HERDR_PLUGIN_STATE_DIR` for experiments. Keep participating script invocations on the same state directory. A direct-script override does not change the state directory used by Herdr's asynchronous event processes; do not assume their records are in your isolated directory.
- Herdr auto-creates a primary workspace for a repository during `worktree create`. Track experiment-owned workspace ids and close that primary workspace after the experiment as well as any remaining experiment-owned worktree workspace. Never close unrelated workspaces.
- Keep lifecycle fixtures and cleanup scoped to the throwaway repository; never edit user control files or delete unrelated state to reconcile an experiment.
