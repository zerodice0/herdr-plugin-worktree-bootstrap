# Herdr Worktree Bootstrap

`zerodice0.worktree-bootstrap` prepares new [Herdr](https://herdr.dev/) Git worktrees, inventories active and removed checkouts, and reviews branches and leftover files after removal. Repository-local lifecycle commands can run after creation, before a plugin-managed removal, and after any observed removal event. Agents can inspect and manage worktrees directly through a non-interactive JSON CLI.

It is deliberately conservative:

- only repository-relative paths listed by the user are considered;
- only Git-ignored, untracked paths are copied;
- directories replace the target directory instead of merging into it;
- lifecycle commands are argv arrays, run sequentially without a shell, and stop on the first failure;
- branch retention is the default, safe deletion uses `git branch -d`, and force deletion requires the exact branch name;
- leftover checkout files are never deleted automatically, and deletion requires verified provenance plus exact-name confirmation;
- there is no ecosystem detection and no automatic selection of dependency or build output directories.

The plugin supports Herdr 0.7.0 or newer on Linux and macOS. It requires Python 3.9 or newer and uses only the Python standard library. It does not require `jq` or third-party Python packages. When `fzf` is available, the management popup uses it as an optional multi-select path picker; the typed-path fallback remains available without it.

## Install

Install the tagged release from GitHub:

```sh
herdr plugin install zerodice0/herdr-plugin-worktree-bootstrap --ref v0.6.1
```

For local development, link this checkout:

```sh
herdr plugin link .
```

Copying and lifecycle commands are opt-in per repository. If neither control file exists in the primary checkout, worktree creation does not change project files. Branch and leftover-file cleanup never happens without an explicit interactive choice or confirmed headless decision.

## Copy local files

Create `.herdr/worktree-copy.list` in the repository's primary checkout. Write one repository-relative path per line:

```text
# Local environment and generated credentials
.env
.dev/certificates

# Spaces and Unicode are supported
local data/settings.json
로컬/설정.json
```

Blank lines and lines beginning with `#` are ignored. The path must exist in the primary checkout and must be ignored by Git. Tracked paths and unignored paths are shown with a reason by `status` and are skipped.

The following entries are rejected before anything is copied:

- `.`, absolute paths, empty path components, and `.` or `..` components;
- duplicates and overlapping parent/child entries such as `cache` plus `cache/npm`;
- paths whose source or target parent traverses a symbolic link.

A listed symbolic link is copied as a link, including broken and external-target links; it is never dereferenced. Symbolic links inside a copied directory are also preserved as links.

The management popup creates the copy list when the first path is added and adds only `/.herdr/worktree-copy.list` to the repository's shared `.git/info/exclude`. It never modifies the tracked `.gitignore`. If you create the control file manually, keep it local by adding the same anchored entry yourself.

## Configure lifecycle commands

Create `.herdr/worktree-setup.json` in the primary checkout:

```json
{
  "version": 2,
  "hooks": {
    "on_created": [
      {
        "name": "Install dependencies",
        "argv": ["npm", "ci"],
        "timeout_seconds": 900
      }
    ],
    "before_remove": [
      {
        "name": "Stop project services",
        "argv": ["./tools/stop-local-services"],
        "timeout_seconds": 60
      }
    ],
    "after_remove": []
  }
}
```

`on_created` runs in the new worktree after local files are copied. `before_remove` runs in an active worktree only when removal is started from **Worktree inventory** or the direct `remove-worktree` action; a failure blocks removal. `after_remove` runs in the primary checkout after a `worktree.removed` event, including removals started outside the plugin. Version 1 files containing `commands` remain supported and are interpreted as `on_created` commands; the editors migrate them to version 2 when they next save.

Commands run in array order. A non-zero exit, timeout, or missing executable stops the phase immediately. The plugin does not invoke a shell, expand environment variables in argv, or implement platform branches. Long-running processes such as development servers and file watchers are not suitable lifecycle commands.

Every phase receives these string environment variables:

| Variable | Meaning |
| --- | --- |
| `HERDR_WORKTREE_PATH` | Absolute checkout path being created or removed. |
| `HERDR_PRIMARY_PATH` | Absolute primary checkout path. |
| `HERDR_WORKTREE_BRANCH` | Local branch, or an empty string for detached/unknown state. |
| `HERDR_WORKTREE_HEAD` | Known worktree HEAD, or an empty string. |
| `HERDR_REMOVAL_FORCED` | `1` for a forced removal, otherwise `0`. |
| `HERDR_LEFTOVER_DETECTED` | `1` when files remain after removal, otherwise `0`. |

This file is also local opt-in configuration. When the management popup sees it, the popup adds `/.herdr/worktree-setup.json` to `.git/info/exclude`.

Use `on_created` for terminating jobs such as `npm ci`, `bundle install`, or `flutter pub get`. Use `before_remove` to stop project-scoped writers that could recreate ignored build output during removal. Commands should be safe to retry: if Herdr refuses removal, a later Inventory or `remove-worktree` attempt runs `before_remove` again.

## Actions

The plugin exposes these Herdr actions and one CLI-only removal action:

| Action | Behavior |
| --- | --- |
| `bootstrap` | Copy eligible paths, then run `on_created` commands. Commands never run if copying fails. |
| `sync` | Copy eligible paths only. |
| `setup` | Run `on_created` commands only. |
| `status` | Validate both control files and show source/target presence, skip reasons, and the last result. |
| `manage` | Open a theme-aware responsive dashboard for files, setup commands, status, and manual sync. |
| `configure-setup` | Open the setup command editor directly. |
| `review-branch-cleanup` | Review branch cleanups that could not be shown immediately or were skipped. |
| `worktree-inventory` | Reconcile Git registrations, branches, recorded provenance, and checkout paths; configure lifecycle phases and start reviewed removal or residue cleanup. |
| `remove-worktree` (CLI only) | Request confirmed Herdr removal after provenance, workspace, dirty-state, and `before_remove` checks. Not a manifest action; invoke the script directly. |

Invoke an action from the Herdr UI, or from the CLI:

```sh
herdr plugin action invoke zerodice0.worktree-bootstrap.status
herdr plugin action invoke zerodice0.worktree-bootstrap.manage
herdr plugin action invoke zerodice0.worktree-bootstrap.configure-setup
herdr plugin action invoke zerodice0.worktree-bootstrap.review-branch-cleanup
herdr plugin action invoke zerodice0.worktree-bootstrap.worktree-inventory
```

The management popup follows the active Herdr theme and uses the Herdr popup frame as its only border. It uses a bounded 96-column by 24-row popup instead of scaling indefinitely with large terminals; Herdr constrains fixed-cell popups to the available client area on smaller screens. The direct setup editor uses a smaller 84-column by 20-row popup. The default dashboard includes every selected copy-plan entry and every setup command without filling the screen with tracked files. When the content exceeds the viewport, a persistent `SCROLL` row shows the visible range, total row count, and position track. Press `j` or Down Arrow to move down and `k` or Up Arrow to move up; Page Up and Page Down move by one viewport. Press `v` to reveal eligible root paths and blocked-path counts. The action footer stays on one line when it fits and wraps only at complete action boundaries on narrower terminals. Navigation redraws are batched in place instead of clearing the terminal between cursor movements.

Management actions check the selected workspace before opening a popup. If it is not a Git repository, the plugin skips the popup and shows a native Herdr notification with an actionable message instead. This avoids a popup that briefly flashes and closes, and follows the current Herdr toast theme and delivery settings. Automated worktree events remain quiet.

Press `a` to select one or more ignored paths with `fzf`. Git supplies the candidates, and wholly ignored directories are collapsed into one row instead of recursively expanding large cache or dependency trees. Inside the picker, use `j`/`k` or Up/Down Arrow to move, Space to toggle multiple paths, Enter to apply, `/` to enter fuzzy-search mode, and Esc to cancel immediately and return to the management screen. Press `d` to open the same picker for currently registered paths; removing an entry stops future copies but never deletes the source file. Press `i` only when an exceptional nested path needs to be typed directly.

The copy plan is opt-in: every path not listed in `.herdr/worktree-copy.list` is already excluded, so there is no separate "do not copy" list. If `fzf` is unavailable, `a` and `d` fall back to the typed path and numbered removal flows. Dashboard choices use a single key without Enter; free-form fields still use Enter because they accept multi-character values.

For a short Herdr-native trigger, add this optional binding to `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+b"
type = "plugin_action"
command = "zerodice0.worktree-bootstrap.manage"
description = "worktree bootstrap settings"
```

Validate and reload it without restarting Herdr:

```sh
herdr config check
herdr server reload-config
```

For example, when `[keys].prefix` is `"ctrl+p"`, press `Ctrl+P` and then `B`. The same dashboard remains searchable as **Worktree bootstrap settings** from Herdr's plugin actions menu.

### Configure creation commands in the management popup

Open the management popup and press `c`, or invoke `configure-setup` directly. This editor manages the `on_created` phase. The setup editor supports single-key choices:

```text
[n] new       [e] edit       [x] delete
[u] move up   [m] move down  [r] run now
[z] remove configuration    [q] back
```

The setup editor uses the same `j`/`k` and arrow-key scrolling as the main dashboard. Reordering uses `u` and `m` so navigation never changes configuration or opens an input prompt.

When adding or editing a command, enter its display name, one argv command line, and a positive timeout. Quotes are parsed into argv entries; no shell is invoked and environment variables are not expanded. For example, entering `npm ci` stores two argv entries, `npm` and `ci`, while `tool "local data/input.json"` keeps the path as one argument. Each change is validated and atomically saved to `.herdr/worktree-setup.json`; deleting the configuration requires confirmation.

`r` runs the saved setup immediately in the current target worktree. The same fail-fast, timeout, and command-not-found rules used by automatic bootstrap apply to this manual run.

## Headless and agent use

Agents should invoke the Python script directly with an explicit target:

```sh
python3 <plugin root>/worktree_bootstrap.py <action> --target <path>
```

`herdr plugin action invoke` always targets the focused Herdr workspace, not the caller's working directory or a supplied workspace environment variable. Calling the script directly avoids changing user workspace focus and returns output to the caller. Locate the installed plugin root with `herdr plugin list`; for a linked checkout, the root is that checkout itself. A GitHub install may, for example, live at `~/.config/herdr/plugins/github-installations/<id>-<hash>/<n>/checkout`; do not assume this path is fixed.

### Read-only inspection

```sh
python3 <plugin root>/worktree_bootstrap.py status --target <path> --json
python3 <plugin root>/worktree_bootstrap.py inventory --target <repo> --json
python3 <plugin root>/worktree_bootstrap.py branch-cleanup --list --json
```

`status --json` returns `source`, `target`, `common_git_dir`, `copy_list`, `lifecycle`, `last_run`, `pending_branch_cleanups` (a count), and `branch_cleanup_popup`. Copy-list entries include source/target presence, eligibility, detail, and whether copying is allowed. Lifecycle data includes the configured argv arrays and timeouts for all three phases. Missing control files are valid empty configurations; invalid files have `valid: false`, an `error` message, and empty entry/command lists.

`inventory --json` returns `repo_root`, `worktree_root`, `items`, and `pending_branch_cleanups` (the requests for that repository). Each item includes registration, branch, creation/removal provenance, lifecycle results, a `leftover` inspection, `removable`, and `confirm_token`. The token is the branch name, or the directory name for a detached worktree. Inventory JSON and cleanup listing never read stdin, open a popup, or resolve pending requests.

`branch-cleanup --list` lists pending requests for all repositories; use `inventory --target <repo> --json` for repository-scoped pending requests. Add `--json` to the cleanup listing for each request's `id`, repository/worktree paths, branch, creation time, forced-removal and provenance flags, saved outcomes, `branch_state`, `inspection_error`, and `leftover` inspection. Without `--json`, it prints one short line per request.

### Confirmed removal and cleanup

```sh
python3 <plugin root>/worktree_bootstrap.py remove-worktree --target <worktree path> --confirm <token> --json
python3 <plugin root>/worktree_bootstrap.py remove-worktree --target <worktree path> --confirm <token> --force --json
```

`--confirm` must exactly match the inventory item's `confirm_token`, including case. The primary checkout cannot be removed. The target must be an active registered worktree with verified provenance and an identifiable Herdr workspace. A dirty checkout additionally requires `--force`; this does not bypass provenance, lifecycle, or other safety checks. The plugin runs `before_remove`, rechecks dirty state, and requests removal through Herdr. Direct `herdr worktree remove` does not run this pre-remove phase.

Success returns `worktree_path`, `repo_root`, `branch`, `workspace_id`, `forced`, `branch_cleanup_id`, and the `removal` result. `branch_cleanup_id` identifies the cleanup request that the asynchronous removal event will queue; it is `null` for a detached worktree. Successful removal is not cleanup completion: removal does not itself delete the branch or decide what to do with leftover files.

```sh
python3 <plugin root>/worktree_bootstrap.py branch-cleanup --target <repo> --id <cleanup-id> --decision keep --json
python3 <plugin root>/worktree_bootstrap.py branch-cleanup --target <repo> --id <cleanup-id> --decision delete --confirm <branch> --json
python3 <plugin root>/worktree_bootstrap.py branch-cleanup --target <repo> --id <cleanup-id> --decision force --confirm <branch> --json
```

Decision mode never reads stdin. Supply `--id`, or set `HERDR_BRANCH_CLEANUP_ID`. `keep` needs no confirmation; both `delete` (safe `git branch -d`) and `force` require the exact branch name in `--confirm`. Protected branches and branches checked out in another worktree remain undeletable. A safe-deletion refusal leaves the request pending rather than escalating to force. If the branch is already absent, including when another actor deletes it before the deletion check, the outcome is `already_absent`, not a claim that this command deleted it.

Branch and leftover-file decisions are independent. If checkout files remain and no `--checkout` is supplied, a successful branch decision is saved with `resolved: false` and `needs: "checkout_decision"`. Resume the same request:

```sh
python3 <plugin root>/worktree_bootstrap.py branch-cleanup --target <repo> --id <cleanup-id> --checkout keep --json
python3 <plugin root>/worktree_bootstrap.py branch-cleanup --target <repo> --id <cleanup-id> --checkout remove --confirm-checkout <directory name> --json
```

`--checkout remove` retains all provenance, configured-root, non-Git-directory, and device/inode checks and requires the exact directory name. A branch decision and checkout decision can also be supplied in one invocation. If no branch outcome is saved and the branch still exists, `--decision` is required even when `--checkout` is supplied. An absent leftover is recorded as `absent`; keeping it records `kept`, and deleting it records `removed`. The response includes `id`, `branch`, `worktree_path`, `branch_outcome`, `checkout_outcome`, `resolved`, and `needs`. Branch outcomes are `kept`, `deleted_safely`, `deleted_forcibly`, or `already_absent`.

Concurrent cleanup decisions are reconciled against the current pending request. Saved branch and checkout outcomes are preserved rather than overwritten, and a completed request is not recreated. A successful branch decision remains saved if a later checkout operation fails; inspect the remaining pending state before resuming.

Without `--list`, `--decision`, or `--checkout`, `branch-cleanup` retains the interactive review. `--json` alone is not a decision mode. `--list` cannot be combined with decision flags. Cleanup-only flags cannot be used with other actions; `--confirm` is shared with `remove-worktree`, and `--force` is only for `remove-worktree`.

### JSON responses and exit codes

`--json` is supported only by `status`, `inventory`, `remove-worktree`, and `branch-cleanup`. Each response is exactly one JSON object on stdout, followed by a newline, with `schema_version: 1`, `action`, and `ok`. Paths are strings, tuples are arrays, and absent values are `null`. Operational failures add `error: {"code": "...", "message": "..."}` with `ok: false`. Invalid control files in a status response instead appear under `copy_list` or `lifecycle`. In JSON mode, pre-remove child stdout is redirected to stderr so it cannot corrupt the response. Status intentionally exposes configured argv; do not treat its output as a secret-free log.

| Error code | Meaning |
| --- | --- |
| `nothing_to_do` | The target is unavailable or is not an applicable Git repository; no notification is opened in JSON mode. |
| `primary_checkout` | Removal targeted the primary checkout. |
| `not_active` | Removal target is not an active registered inventory item. |
| `confirmation_mismatch` | The exact branch or directory confirmation does not match. |
| `dirty_requires_force` | Dirty worktree removal requires `--force`. |
| `provenance_missing` | Removal lacks verified worktree provenance. |
| `workspace_not_found` | The target has no identifiable Herdr workspace. |
| `before_remove_failed` | A pre-remove lifecycle command failed. |
| `dirty_after_before_remove` | The post-hook dirty-state check blocked removal. |
| `herdr_failed` | Herdr is unavailable or refused removal. |
| `cleanup_id_required` | Decision mode has no cleanup id. |
| `cleanup_not_found` | The pending cleanup id does not exist yet or no longer exists. |
| `invalid_record` | The cleanup record is invalid. |
| `inspection_failed` | Branch inspection failed; the request remains pending. |
| `decision_required` | An existing branch still needs a decision. |
| `branch_protected` | The branch is protected. |
| `branch_in_use` | Another worktree uses the branch. |
| `safe_delete_refused` | Git refused safe deletion; the request remains pending. |
| `force_delete_failed` | Forced branch deletion failed; the request remains pending. |
| `checkout_delete_failed` | Leftover-file deletion failed; the request remains pending. |
| `error` | Other operational failure. |

Successful commands return exit code `0`; a partial cleanup with `resolved: false` also returns `0`. Require `ok: true` and `resolved: true`, and inspect both outcomes, to establish headless cleanup completion—not exit code alone. `nothing_to_do` returns `0` despite `ok: false`. Operational failures normally return `1`. Status returns `2` when either control file is invalid. Invalid flag combinations, missing required flags, and other argparse usage errors return `2` with usage text on stderr, not a JSON envelope.

### Disable removal popups

For unattended operation, we recommend setting `branch_cleanup_popup` to `"never"` in `settings.json` in the plugin configuration directory, preserving any other settings. This is an operator choice; the plugin does not change this setting automatically.

```json
{"version": 1, "branch_cleanup_popup": "never"}
```

The directory is selected in this order:

1. `HERDR_PLUGIN_CONFIG_DIR`, when set;
2. `$XDG_CONFIG_HOME/herdr/plugins/config/zerodice0.worktree-bootstrap`;
3. `~/.config/herdr/plugins/config/zerodice0.worktree-bootstrap`.

Herdr sets `HERDR_PLUGIN_CONFIG_DIR` for plugin processes; `herdr plugin list` shows it as `config:`. Configure the directory used by Herdr itself to suppress its event popups, not just the environment of a direct script invocation.

`branch_cleanup_popup` accepts `"auto"` or `"never"`. A missing file or key defaults to `"auto"`. Invalid JSON, types, or values produce a warning on stderr without file contents and fall back to `"auto"`. `"never"` suppresses both the removal-event popup and notification, but still queues the request and runs `after_remove`; the event reports `queued branch cleanup for <branch>; popup=disabled` and `popup_opened: false`. Status reports the effective mode. This setting does not disable an explicitly requested interactive review.

### End-to-end example

Choose one cleanup owner: for headless operation, configure the removal popup mode to `"never"` as above, then use explicit repository and worktree paths. Alternatively, let the interactive review own cleanup. Do not concurrently run popup decisions, headless decisions, or direct `git branch` deletion for the same request.

```sh
herdr worktree create --cwd <repo> --branch <b> --base main --path <p> --no-focus
python3 <plugin root>/worktree_bootstrap.py status --target <p> --json
python3 <plugin root>/worktree_bootstrap.py inventory --target <repo> --json
python3 <plugin root>/worktree_bootstrap.py remove-worktree --target <p> --confirm <confirm_token> --json
python3 <plugin root>/worktree_bootstrap.py branch-cleanup --target <repo> --id <branch_cleanup_id> --decision delete --confirm <b> --json
```

Use the exact `confirm_token` from inventory for removal, then the `branch_cleanup_id` returned by removal for cleanup; a detached worktree has no branch cleanup ID. Herdr events are asynchronous: creation/bootstrap may still be running when the worktree first appears, and the cleanup request may not yet exist when removal returns. Wait for creation/bootstrap to finish before removal. On `cleanup_not_found` immediately after removal, briefly retry only within a fixed, short deadline while the removal event catches up. The same error also means an already-resolved or otherwise absent request: if it persists, inspect inventory and the pending list rather than assuming success or retrying indefinitely. Do not retry unrelated failures blindly or escalate a safe-delete refusal automatically. If files remain, make the separate `--checkout` decision above; completion requires `ok: true` and `resolved: true`.

Herdr may also create a primary workspace for the repository during `worktree create`; close that experiment-owned workspace afterward. For smoke tests, use throwaway repositories under `/tmp`, preserve user workspace focus, and never close unrelated workspaces. Direct-script experiments can use `--state-dir <temporary directory>` or `HERDR_PLUGIN_STATE_DIR`; all invocations participating in the same experiment must see the relevant records. Herdr's asynchronous event processes use their own configured plugin state directory, not a direct invocation's override.

## Worktree inventory

Open **Worktree inventory and lifecycle** to reconcile three independent sources of truth:

- Git registrations from `git worktree list`;
- local branches and whether they still exist;
- recorded creation/removal provenance and actual checkout paths, including ignored residue under `[worktrees].directory` (default `~/.herdr/worktrees`).

Each row reports the branch, Git registration, path presence, provenance status, creation commit, candidate base refs, lifecycle results, and a bounded residue summary. Git does not store the original base branch after worktree creation, so “base candidates” are the other local or remote refs that pointed at the creation commit when the plugin observed `worktree.created`; they are evidence, not a guaranteed ancestry claim.

| Inventory state | Meaning |
| --- | --- |
| `primary` | Primary non-bare checkout. It cannot be removed here. |
| `active` | Git registration and a real checkout directory both exist. |
| `missing` | Git still registers the worktree, but its checkout path is missing or unsafe. |
| `branch-only` | The worktree is no longer registered and no checkout path remains, but its local branch exists. |
| `residue` | The worktree is no longer registered, but a path remains and can block recreation. |
| `removed` | Recorded worktree, branch, and checkout path are all absent. |

Inventory can start Herdr removal for an active worktree. It checks dirty state, requires typing the exact branch or directory name, runs `before_remove`, checks dirty state again, and then calls `herdr worktree remove`. A dirty checkout is removed with `--force` only after that explicit confirmation. Removal started from Herdr's built-in action or another tool cannot run `before_remove`; use Inventory or the direct `remove-worktree` action when the pre-remove phase matters.

Residue cleanup is a separate action from branch cleanup. It is offered only for a path whose provenance was recorded while it was a worktree. Before deletion, the plugin rechecks that the path is strictly below the absolute `[worktrees].directory`, its canonical parent has not changed, it is a real directory rather than a symbolic link, it is not registered by Git, it is not recognized as any Git repository, and it still has the displayed device/inode. The exact directory name must then be typed. Directories merely discovered by scanning the configured root are shown as `unverified` and cannot be deleted by the plugin. A residue outside the configured root remains visible but must be handled manually.

Removing residue also updates a matching pending cleanup without overwriting its saved branch outcome. If that branch still needs a decision, the cleanup remains pending for branch review.

Choose **Lifecycle** inside Inventory to add, edit, delete, or reorder commands for all three phases. Changes are validated and atomically saved in the version 2 schema above.

Inventory intentionally stays open after removal or a nested cleanup review so you can continue managing other worktrees. Return with its Back/Quit action when finished; this is different from a standalone cleanup review completing.

## Automatic worktree bootstrap

The `worktree.created` event records the creation commit and candidate base refs, then runs `bootstrap`. Herdr event commands are post-creation asynchronous hooks: the worktree and its initial pane already exist when bootstrap starts. Do not depend on bootstrap completing before a separate automatic build or development server starts. That kind of cross-hook ordering is not provided by this plugin.

The source is always the primary non-bare checkout from `git worktree list --porcelain`. The plugin verifies that source and target share the same common Git directory. It safely does nothing for the primary checkout itself, bare repositories, unavailable primary checkouts, or repositories with no control files.

## Branch cleanup after worktree removal

Herdr intentionally removes a worktree checkout without deleting its branch. This plugin listens for `worktree.removed`, records a pending cleanup request, inspects the local branch and checkout path independently, runs `after_remove`, and attempts to open a session-modal terminal popup unless `branch_cleanup_popup` is `"never"` (see [Headless and agent use](#headless-and-agent-use)).

The popup shows:

- repository, removed worktree, branch, and last commit;
- whether the branch is merged into the detected default branch;
- upstream and ahead/behind information when available;
- whether another worktree still uses the branch;
- whether the worktree itself was removed with `--force`;
- whether the removed checkout path has reappeared or still contains files, with bounded file-count, size, and path samples.

The choices are:

- **Keep** (default): retain the branch and save that branch decision.
- **Delete safely**: run `git branch -d`; Git refuses deletion when the branch is not merged according to its normal safety rules.
- **Force delete**: display an unmerged-commit warning and require typing the exact branch name before running `git branch -D`.
- **Skip**: keep the request pending for later review.

After the branch decision, any leftover checkout directory gets its own decision: keep it, remove it after exact-name confirmation, or leave it pending. Keeping or deleting a branch never implies deleting checkout files, and keeping or deleting checkout files never changes a branch.

In the interactive popup, press `D`, `F`, `S`, or `Q` to act immediately; no Enter is required. Press Enter by itself to keep the branch. The action footer stays on one line when it fits and wraps into balanced, complete action groups on narrower terminals. The exact branch-name confirmation for force deletion intentionally remains a typed value followed by Enter. If direct key reading is unavailable, the popup falls back to the same choices through normal line input. Non-TTY and caller-supplied line input remain blocking, without periodic refresh; supplied input is not bypassed to read from real stdin.

While a real-TTY branch or leftover-file action menu is idle, it rechecks the pending request, branch, and checkout path every second and redraws only when their state changes. An externally deleted branch is recorded as `already_absent` if no branch outcome was already saved. Remaining checkout files still require their own decision; a timeout never authorizes deletion. If another reviewer saves a partial decision, the menu shows the remaining work instead of applying a stale choice.

Typed branch and directory confirmations are not refreshed or interrupted. After input finishes, the plugin reloads the request and rechecks the displayed branch commit and directory device/inode before mutation. If the request was replaced, or the branch commit or directory identity changed, the old approval cannot authorize deletion; review the current state again.

`main`, `master`, `develop`, `development`, `trunk`, the detected default branch, and any branch checked out in another worktree cannot be deleted from the popup. Branch state and the displayed commit are checked again immediately before deletion. Remote branches are displayed through upstream status but are never deleted.

The hook runs after Herdr has attempted removal, so a process such as Xcode can recreate an ignored directory after Git has unregistered the worktree. The review re-inspects the path when it opens instead of trusting event-time state. If no foreground client exists or another popup is busy, the handler never deletes anything; the unresolved request remains under `HERDR_PLUGIN_STATE_DIR` and can be reopened with:

```sh
herdr plugin action invoke zerodice0.worktree-bootstrap.review-branch-cleanup
```

A standalone review finishes once its selected request is resolved or no longer pending; if another actor already completed it, the review stops prompting without recreating it. Herdr owns dismissal of the popup when its command exits; the plugin does not send a separate close command. Leaving with Skip, Quit, or end-of-input can still leave pending work, so process exit alone is not proof of cleanup completion or of the final rendered popup disappearing.

The plugin caches worktree-to-primary-checkout provenance when `worktree.created` fires so removal events with a missing workspace snapshot can still be reviewed safely. Detached worktrees have no local branch cleanup, but their removal history and `after_remove` result remain visible in Inventory. If neither the removal event nor that cache can identify and validate the primary checkout, the event is logged and no cleanup request, lifecycle command, or deletion is attempted.

### Theme-aware cleanup dialog

In an interactive Herdr popup, the cleanup review follows Herdr's configured built-in theme and supported semantic overrides from `[theme.custom]`. It uses Herdr's popup frame as the only border, re-reads the theme when each popup opens, keeps the terminal background transparent, and uses Herdr's light/dark sibling selection when `theme.auto_switch` is enabled. The current built-in themes and aliases from Herdr 0.7.5 are supported.

The plugin reads only `name`, `auto_switch`, `dark_name`, and `light_name` from `[theme]`, supported color strings from `[theme.custom]`, and the legacy `ui.accent` fallback. It never logs configuration contents. Unknown themes, unsupported TOML forms, unavailable terminal appearance responses, `NO_COLOR`, non-interactive output, and narrow terminals fall back safely without affecting branch inspection or deletion rules.

## Copy and failure guarantees

Before replacing a configured path, the plugin stages every eligible source. Existing target entries are then renamed to transaction backups and staged entries are renamed into place. If any commit step fails, already replaced entries are restored in reverse order. Because directories are replaced as whole entries, target-only stale files disappear after a successful sync.

Per-target locks live under `HERDR_PLUGIN_STATE_DIR`, preventing a manual action and an event from changing the same target concurrently. Interrupted or stale target transaction directories are cleaned up on the next run. File modes and executable bits are preserved.

Plugin logs contain command names, counts, skip reasons, failures, and exit codes. The plugin does not print file contents, argv values, environment values, or credential values. Child process stdout and stderr still flow to Herdr's command log, so lifecycle commands remain responsible for not printing secrets.

Inspect recent Herdr command logs with:

```sh
herdr plugin log list --plugin zerodice0.worktree-bootstrap --limit 20
```

## Choosing what to copy

Prefer `on_created` commands for reproducible dependency and build outputs. Do not add `node_modules`, `.dart_tool`, build trees, or caches merely because they are ignored. Copy only local state that cannot be regenerated cheaply and that you intentionally want duplicated into every new worktree.

## Upgrade and rollback

Avoid enabling this plugin alongside an older local worktree-copy hook; both would respond to the same creation event.

Before validation, find and disable the old plugin:

```sh
herdr plugin list
herdr plugin disable OLD_PLUGIN_ID
herdr plugin install zerodice0/herdr-plugin-worktree-bootstrap --ref v0.6.1
```

To roll back, disable this plugin and re-enable the previous one. Control files are left untouched:

```sh
herdr plugin disable zerodice0.worktree-bootstrap
herdr plugin enable OLD_PLUGIN_ID
```

## Development

Run the complete standard-library test suite:

```sh
python3 -m unittest discover -s tests -v
python3 tests/herdr_environment_smoke.py
```

CI covers Ubuntu and macOS with Python 3.9 through 3.14. A separate smoke job downloads Herdr 0.7.5, validates `herdr-plugin.toml` with `herdr plugin link`, and exercises both action-context and event-context JSON resolution.

## License

MIT. See [LICENSE](LICENSE).
