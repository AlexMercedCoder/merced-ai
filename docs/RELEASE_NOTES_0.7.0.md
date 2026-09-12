# merced-ai 0.7.0

Release notes — September 12, 2026.

- Conversation appends are transactional, carry stable turn IDs, and preserve concurrent accepted responses. Stale full-record saves fail with an explicit conflict; deleted conversations cannot be silently recreated by stale clients.
- The broker owns runs independently of HTTP streams, journals bounded replay events, supports reconnect by run ID and sequence, and marks abandoned owner records interrupted on startup.
- Cancellation and timeout stop owned process groups on POSIX and request tree termination on Windows. Incremental stdout/stderr capture is bounded, including an independent bounded AAIS control-frame path.
- Session profile spec digests are checked before dispatch in group and resumed CLI conversations. Replies record the actual profile revision and digests; state-only changes do not trigger authority drift.
- The approval presenter restores live requests, keeps idempotent decisions and authority receipts, and distinguishes orphaned requests during recovery.
- Required WebMCP routes use installed native capability negotiation; unknown/old installations fail conservatively. Authenticated registry and final policy checks remain in each harness.
- The browser reconnects to an owned run, restores saved run observation after reload, uses truthful completion status, and wraps header controls to avoid inspector overlap.

Existing conversation files load without a rewrite. Back up `.merced-ai` before upgrading; keep that backup for rollback to 0.6.0. A server restart marks abandoned work interrupted and never automatically repeats a possibly mutating action.

AGS, OAP and AAIS document/wire formats remain unchanged. Local validation evidence and remaining platform gates are recorded in the ecosystem release report.
