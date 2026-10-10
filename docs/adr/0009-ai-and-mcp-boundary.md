# ADR-0009: AI and MCP boundary — AI configures, humans approve, runtime is deterministic

- Status: Accepted · Date: 2026-10-10

## Context
AI accelerates onboarding (reading a product's description/OpenAPI and proposing a manifest). AI output is probabilistic and must never move money or silently change financial rules.

## Decision
Pipeline: **natural language → AI → structured manifest draft → JSON Schema validation (+ repair loop, bounded) → simulation → human approval → versioned active config → deterministic runtime.**
- The AI Integration Analyst uses **Google Gemini Flash-Lite** (free tier; `FH_AI_MODEL`, default `gemini-3.5-flash-lite`) through the official `google-genai` SDK behind an `LLMClient` port, with JSON-schema structured output targeting `manifest.v1`; outputs include rationale and open questions. Free-tier quotas are handled with backoff and a clear "AI unavailable" state; without a key the analyst runs in a labeled *Simulated* mode with canned proposals. Swapping the model provider is an adapter change.
- **Fixture mode** (no API key) returns recorded proposals and is labeled *Simulated* in UI/docs.
- MCP server (streamable HTTP, API key with scope `mcp`) exposes: `capabilities`, `get_manifest_schema`, `submit_system_profile`, `propose_manifest`, `validate_manifest`, `diff_manifest`, `simulate_manifest`, `submit_for_approval`, and read-only queries (customers, invoices, balances, events). **No tool can approve, activate, post ledger entries, create payments, refunds or payouts.** A test asserts the tool list against an allow-list.
- All AI/MCP actions are audited with the API key / operator identity.

## Consequences
+ Clear safety story; demonstrable governance. − Approval is a manual step by design.

## Alternatives
Autonomous AI activation (unacceptable for money); no AI (misses the onboarding goal).
