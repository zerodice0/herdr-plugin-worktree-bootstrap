# Herdr Worktree Bootstrap

`zerodice0.worktree-bootstrap` prepares new [Herdr](https://herdr.dev/) Git worktrees by copying explicitly selected local files and then running explicitly configured setup commands.

It is deliberately conservative:

- only repository-relative paths listed by the user are considered;
- only Git-ignored, untracked paths are copied;
- directories replace the target directory instead of merging into it;
- setup commands are argv arrays, run sequentially without a shell, and stop on the first failure;
- there is no ecosystem detection and no automatic selection of dependency or build output directories.

The plugin supports Herdr 0.7.0 or newer on Linux and macOS. It requires Python 3.9 or newer and uses only the Python standard library. It does not require `jq` or third-party Python packages.

## Install

Install the tagged release from GitHub:

```sh
herdr plugin install zerodice0/herdr-plugin-worktree-bootstrap --ref v0.1.0
```

For local development, link this checkout:

```sh
herdr plugin link .
```

The plugin is opt-in per repository. If neither control file exists in the primary checkout, all automatic events exit successfully without changing anything.

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

Invoke an action from the Herdr UI, or from the CLI:

```sh
herdr plugin action invoke zerodice0.worktree-bootstrap.status
herdr plugin action invoke zerodice0.worktree-bootstrap.manage
```

The management popup scans only direct children of the repository root. It labels them as included, addable/ignored, tracked, or unignored. To add a nested path, type its repository-relative path; ignored directories are not recursively walked.

## Automatic worktree bootstrap

The `worktree.created` event runs `bootstrap`. Herdr event commands are post-creation asynchronous hooks: the worktree and its initial pane already exist when bootstrap starts. Do not depend on bootstrap completing before a separate automatic build or development server starts. That kind of cross-hook ordering is not provided by this plugin.

The source is always the primary non-bare checkout from `git worktree list --porcelain`. The plugin verifies that source and target share the same common Git directory. It safely does nothing for the primary checkout itself, bare repositories, unavailable primary checkouts, or repositories with no control files.

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
herdr plugin install zerodice0/herdr-plugin-worktree-bootstrap --ref v0.1.0
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
