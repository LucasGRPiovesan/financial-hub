# ADR-0010: Realtime via Server-Sent Events + Postgres LISTEN/NOTIFY

- Status: Accepted · Date: 2026-10-10

## Context
The console needs live updates (payments confirmed, deliveries, ledger activity). Traffic is server → client only. Options: WebSockets, SSE, Centrifugo (research §3).

## Decision
- SSE endpoint `/v1/stream` (session-authenticated, project-scoped, permission-filtered).
- Fan-out across API replicas through `LISTEN events` (NOTIFY payload = event id); each stream loads the event and filters by project/permissions.
- Resume with `Last-Event-ID` by reading the event log after that id (bounded window) — the log is the replay buffer.
- Heartbeat comments every 15s; per-connection queue bound with drop-to-resync signal.

## Consequences
+ Plain HTTP (works through proxies, auto-reconnect in browsers, trivial auth with cookies), no extra infra. − One LISTEN connection per API replica; unidirectional only (sufficient).

## Alternatives
WebSockets (bidirectional not needed; more auth/proxy work); Centrifugo/Redis pub-sub (extra services).
