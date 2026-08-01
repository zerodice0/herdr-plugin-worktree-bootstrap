# Changelog

All notable changes to this project are documented here.

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
