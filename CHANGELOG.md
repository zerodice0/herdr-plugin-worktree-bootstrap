# Changelog

All notable changes to this project are documented here.

## [Unreleased]

- Reconcile idle real-TTY branch and leftover-file review menus every second so external changes and completed requests no longer require a keypress to be noticed.
- Preserve typed confirmations without refresh and require fresh review when the displayed branch commit, checkout identity, or cleanup request changes.
- Preserve independent branch and checkout decisions across concurrent reviewers and partial failures without recreating completed requests.
- Report `already_absent` when a branch was removed elsewhere instead of claiming safe or forced deletion.
- Clarify standalone review completion, intentionally persistent Inventory, and single-owner headless cleanup with bounded asynchronous-event retries and explicit completion checks.

## [0.6.0] - 2026-10-16

- Add read-only `status --json` and `inventory --json` responses with a versioned envelope and machine-readable errors.
- Add a `branch_cleanup_popup` setting to suppress removal-event popups and notifications while retaining queued cleanup and `after_remove` commands.
- Add CLI-only `remove-worktree` with exact confirmation, explicit dirty-worktree force, and the same provenance and pre-remove lifecycle safeguards as Inventory.
- Add non-interactive pending-cleanup listing, branch decisions, and separately confirmed leftover-file decisions.
- Document direct-script agent workflows and repository safety conventions in README and AGENTS, with coverage for the headless interfaces.

## [0.5.0] - 2026-10-16

- Add a Worktree Inventory action that reconciles Git registrations, local branches, recorded creation/removal provenance, and checkout paths.
- Record the creation commit and other refs pointing at it as best-effort base candidates for newly observed worktrees.
- Preserve removal tombstones and lifecycle results instead of discarding worktree provenance after removal.
- Detect ignored files or empty directories left after worktree removal and review them independently from local branches.
- Require the configured worktree root, recorded provenance, a non-Git real directory, unchanged device/inode, and exact-name confirmation before deleting leftover files; never delete unverified discovered paths.
- Expand `.herdr/worktree-setup.json` to a version 2 lifecycle schema with `on_created`, `before_remove`, and `after_remove` command phases while retaining version 1 compatibility.
- Run pre-remove commands only for Inventory-managed removal, fail closed on command errors or newly dirty state, and expose structured worktree context through environment variables.
- Run post-remove commands from the primary checkout once per recorded worktree generation.

## [0.4.0] - 2026-08-03

- Show every registered copy path and setup command through a bounded, vertically scrollable viewport.
- Keep a visible range counter and position track on screen whenever more content is available.
- Support `j`/`k`, Up/Down Arrow, and Page Up/Page Down navigation without closing the popup.
- Move setup reordering and configuration removal off `j`/`k` so navigation cannot mutate configuration.
- Add an optional, theme-aware `fzf` picker for multi-select path registration and removal, with collapsed ignored directories and NUL-safe path handling.
- Keep direct path entry as an advanced fallback and clarify that unlisted paths are never copied.
- Batch terminal refreshes in place so dashboard cursor movement does not clear to a blank frame.
- Keep Escape bound to immediate picker cancellation in both navigation and search modes.

## [0.3.3] - 2026-08-03

- Preflight management actions before opening their Herdr popup.
- Show a native Herdr notification instead of flashing and immediately closing a popup when the selected workspace is not a Git repository.
- Normalize non-Git failures into a stable, actionable message instead of exposing locale-dependent `git rev-parse` output.

## [0.3.2] - 2026-08-02

- Bound the main management popup to 96 columns by 24 rows instead of scaling it as a percentage of very large clients.
- Bound the direct setup editor to a compact 84 columns by 20 rows.
- Preserve Herdr's available-area handling for clients smaller than the requested fixed-cell dimensions.

## [0.3.1] - 2026-08-02

- Redesign the management popup as a compact, theme-aware dashboard with clearer copy and setup sections.
- Hide low-value tracked and unignored path rows by default while preserving counts and an opt-in eligible-path view.
- Make dashboard and setup-editor actions single-key, with responsive action wrapping and persistent inline notices.
- Add a direct `configure-setup` Herdr action and pane entrypoint.
- Document an optional Herdr-native `prefix+b` shortcut and live configuration reload.

## [0.3.0] - 2026-08-01

- Add a setup command editor to the management popup.
- Support adding, editing, deleting, reordering, and manually running setup commands without editing JSON by hand.
- Parse quoted command lines into argv arrays without invoking a shell or expanding environment variables.
- Atomically save validated setup configuration and require confirmation before removing it.

## [0.2.3] - 2026-07-31

- Keep the cleanup action footer on one line whenever its measured display width fits.
- Wrap only at complete action boundaries and choose the most balanced layout on narrower terminals.
- Fall back to a visible line-input prompt instead of waiting for an unlabelled key on very narrow TTYs.

## [0.2.2] - 2026-07-31

- Remove the redundant plugin-drawn frame and use Herdr's popup frame as the only border.
- Reduce the branch cleanup popup to 68% width and 50% height.
- Make ordinary cleanup choices react to a single key without requiring Enter.
- Keep exact branch-name input plus Enter for destructive force deletion, with line-input fallback when direct terminal input is unavailable.

## [0.2.1] - 2026-07-31

- Redesign branch cleanup review as a responsive terminal dialog with status badges and a separate force-delete warning card.
- Follow Herdr built-in themes and supported `[theme.custom]` semantic color overrides.
- Match Herdr light/dark sibling selection when `theme.auto_switch` is enabled.
- Preserve plain-text output for non-TTY, `NO_COLOR`, narrow terminal, and theme-detection fallback cases.
- Keep terminal backgrounds transparent so popup chrome remains consistent with the active Herdr client.

## [0.2.0] - 2026-07-31

- Queue local branch cleanup after Herdr emits `worktree.removed`.
- Add a session-modal review popup with keep-by-default, safe delete, and typed force-delete choices.
- Protect default/common branches and branches still used by another worktree.
- Preserve pending cleanup requests when no popup is available and expose a manual review action.
- Cache worktree provenance so removal events without a workspace snapshot can still resolve the primary checkout.
- Never delete remote branches.

## [0.1.0] - 2026-07-31

- Initial public release.
- Copy explicitly listed ignored local files into newly created worktrees.
- Run opt-in argv-based setup commands with timeouts and fail-fast behavior.
- Add transactional replacement, rollback, per-target locking, status, and management popup.
