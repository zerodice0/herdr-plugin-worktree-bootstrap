# Changelog

All notable changes to this project are documented here.

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
