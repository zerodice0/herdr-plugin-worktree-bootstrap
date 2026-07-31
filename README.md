# Herdr Worktree Bootstrap

`zerodice0.worktree-bootstrap` prepares new [Herdr](https://herdr.dev/) Git worktrees by copying explicitly selected local files and running setup commands. After Herdr removes a worktree, it also offers an interactive review before deleting the associated local branch.

It is deliberately conservative:

- only repository-relative paths listed by the user are considered;
- only Git-ignored, untracked paths are copied;
- directories replace the target directory instead of merging into it;
- setup commands are argv arrays, run sequentially without a shell, and stop on the first failure;
- branch retention is the default, safe deletion uses `git branch -d`, and force deletion requires typing the exact branch name;
- there is no ecosystem detection and no automatic selection of dependency or build output directories.

The plugin supports Herdr 0.7.0 or newer on Linux and macOS. It requires Python 3.9 or newer and uses only the Python standard library. It does not require `jq` or third-party Python packages.

## Install

Install the tagged release from GitHub:

```sh
herdr plugin install zerodice0/herdr-plugin-worktree-bootstrap --ref v0.2.2
```

For local development, link this checkout:

```sh
herdr plugin link .
```

Copying and setup are opt-in per repository. If neither control file exists in the primary checkout, worktree creation does not change project files. Branch cleanup review is global but never deletes a branch without an explicit interactive choice.

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

## Run setup commands

Create `.herdr/worktree-setup.json` in the primary checkout:

```json
{
  "version": 1,
  "commands": [
    {
      "name": "Install dependencies",
      "argv": ["npm", "ci"],
      "timeout_seconds": 900
    },
    {
      "name": "Fetch Flutter packages",
      "argv": ["flutter", "pub", "get"],
      "timeout_seconds": 900
    }
  ]
}
```

Every command runs in the target worktree root. Commands run in array order. A non-zero exit, timeout, or missing executable stops the sequence immediately. The plugin does not invoke a shell, expand environment variables in argv, or implement platform branches.

This file is also local opt-in configuration. When the management popup sees it, the popup adds `/.herdr/worktree-setup.json` to `.git/info/exclude`. The setup list is read-only in the popup; edit the JSON file directly.

Use setup for terminating jobs such as `npm ci`, `bundle install`, or `flutter pub get`. Long-running processes such as `npm run dev`, file watchers, and `flutter run` are outside v1's scope.

## Actions

The plugin exposes these Herdr actions:

| Action | Behavior |
| --- | --- |
| `bootstrap` | Copy eligible paths, then run setup commands. Setup never runs if copying fails. |
| `sync` | Copy eligible paths only. |
| `setup` | Run setup commands only. |
| `status` | Validate both control files and show source/target presence, skip reasons, and the last result. |
| `manage` | Open an 80% by 80% popup for add, delete, status, and manual sync. |
| `review-branch-cleanup` | Review branch cleanups that could not be shown immediately or were skipped. |

Invoke an action from the Herdr UI, or from the CLI:

```sh
herdr plugin action invoke zerodice0.worktree-bootstrap.status
herdr plugin action invoke zerodice0.worktree-bootstrap.manage
herdr plugin action invoke zerodice0.worktree-bootstrap.review-branch-cleanup
```

The management popup scans only direct children of the repository root. It labels them as included, addable/ignored, tracked, or unignored. To add a nested path, type its repository-relative path; ignored directories are not recursively walked.

## Automatic worktree bootstrap

The `worktree.created` event runs `bootstrap`. Herdr event commands are post-creation asynchronous hooks: the worktree and its initial pane already exist when bootstrap starts. Do not depend on bootstrap completing before a separate automatic build or development server starts. That kind of cross-hook ordering is not provided by this plugin.

The source is always the primary non-bare checkout from `git worktree list --porcelain`. The plugin verifies that source and target share the same common Git directory. It safely does nothing for the primary checkout itself, bare repositories, unavailable primary checkouts, or repositories with no control files.

## Branch cleanup after worktree removal

Herdr intentionally removes a worktree checkout without deleting its branch. This plugin listens for `worktree.removed`, records a pending cleanup request, inspects the local branch, and attempts to open a session-modal terminal popup.

The popup shows:

- repository, removed worktree, branch, and last commit;
- whether the branch is merged into the detected default branch;
- upstream and ahead/behind information when available;
- whether another worktree still uses the branch;
- whether the worktree itself was removed with `--force`.

The choices are:

- **Keep** (default): retain the branch and resolve the request.
- **Delete safely**: run `git branch -d`; Git refuses deletion when the branch is not merged according to its normal safety rules.
- **Force delete**: display an unmerged-commit warning and require typing the exact branch name before running `git branch -D`.
- **Skip**: keep the request pending for later review.

In the interactive popup, press `D`, `F`, `S`, or `Q` to act immediately; no Enter is required. Press Enter by itself to keep the branch. The exact branch-name confirmation for force deletion intentionally remains a typed value followed by Enter. If direct key reading is unavailable, the popup falls back to the same choices through normal line input.

`main`, `master`, `develop`, `development`, `trunk`, the detected default branch, and any branch checked out in another worktree cannot be deleted from the popup. Branch state is inspected again immediately before deletion to close the race between display and confirmation. Remote branches are displayed through upstream status but are never deleted.

The hook runs after Herdr has removed the worktree. Herdr v1 does not expose a pre-remove interception or native yes/no dialog API, so this plugin cannot modify the built-in removal confirmation. If no foreground client exists or another popup is busy, the handler never deletes anything; the resolved request remains under `HERDR_PLUGIN_STATE_DIR` and can be reopened with:

```sh
herdr plugin action invoke zerodice0.worktree-bootstrap.review-branch-cleanup
```

The plugin caches worktree-to-primary-checkout provenance when `worktree.created` fires so removal events with a missing workspace snapshot can still be reviewed safely. Detached worktrees have no local branch cleanup and are ignored.
If neither the removal event nor that cache can identify and validate the primary checkout, the event is logged and no cleanup request or branch deletion is attempted.

### Theme-aware cleanup dialog

In an interactive Herdr popup, the cleanup review follows Herdr's configured built-in theme and supported semantic overrides from `[theme.custom]`. It uses Herdr's popup frame as the only border, re-reads the theme when each popup opens, keeps the terminal background transparent, and uses Herdr's light/dark sibling selection when `theme.auto_switch` is enabled. The current built-in themes and aliases from Herdr 0.7.5 are supported.

The plugin reads only `name`, `auto_switch`, `dark_name`, and `light_name` from `[theme]`, supported color strings from `[theme.custom]`, and the legacy `ui.accent` fallback. It never logs configuration contents. Unknown themes, unsupported TOML forms, unavailable terminal appearance responses, `NO_COLOR`, non-interactive output, and narrow terminals fall back safely without affecting branch inspection or deletion rules.

## Copy and failure guarantees

Before replacing a configured path, the plugin stages every eligible source. Existing target entries are then renamed to transaction backups and staged entries are renamed into place. If any commit step fails, already replaced entries are restored in reverse order. Because directories are replaced as whole entries, target-only stale files disappear after a successful sync.

Per-target locks live under `HERDR_PLUGIN_STATE_DIR`, preventing a manual action and an event from changing the same target concurrently. Interrupted or stale target transaction directories are cleaned up on the next run. File modes and executable bits are preserved.

Plugin logs contain command names, counts, skip reasons, failures, and exit codes. The plugin does not print file contents, argv values, environment values, or credential values. Child process stdout and stderr still flow to Herdr's command log, so setup commands remain responsible for not printing secrets.

Inspect recent Herdr command logs with:

```sh
herdr plugin log list --plugin zerodice0.worktree-bootstrap --limit 20
```

## Choosing what to copy

Prefer setup commands for reproducible dependency and build outputs. Do not add `node_modules`, `.dart_tool`, build trees, or caches merely because they are ignored. Copy only local state that cannot be regenerated cheaply and that you intentionally want duplicated into every new worktree.

## Upgrade and rollback

Avoid enabling this plugin alongside an older local worktree-copy hook; both would respond to the same creation event.

Before validation, find and disable the old plugin:

```sh
herdr plugin list
herdr plugin disable OLD_PLUGIN_ID
herdr plugin install zerodice0/herdr-plugin-worktree-bootstrap --ref v0.2.2
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
