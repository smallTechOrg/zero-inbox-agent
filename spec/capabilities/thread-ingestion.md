# Capability: Thread Ingestion

## What It Does
Pulls the user's recent inbox **threads** from the connected mailbox and normalizes them into the
channel-agnostic `Item` shape, redacting secrets and persisting only headers, identifiers and a
≤200-character snippet — never a body.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Connection | entity | `channel_accounts` | Yes |
| Limit | int (default 200) | triage request | Yes |
| Date range | timestamp pair | backlog job (Phase 3) | No |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Normalized threads | `Item[]` | `items` table + graph state |
| Sender evidence updates | counts | `sender_profiles` |
| Redaction events | log | structlog |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| Mailbox channel | list inbox threads (metadata/headers only) | Retry 3× with backoff; on persistent failure fail the run with `handle_error` — no thread is decided on missing data |
| Mailbox channel | fetch full thread body (escalation only, in memory) | Fall back to the snippet and mark the item unsure |

## Business Rules
- **Thread-level, never message-level.** One `Item` per thread; the latest message supplies the
  displayed sender and date, and `message_count` reflects the whole thread.
- Extracted headers: `From`, `To`, `Cc`, `Subject`, `Date`, `List-Id`, `List-Unsubscribe`, attachment
  presence, unread state, current labels.
- The snippet is truncated to 200 characters and passed through the redactor **before** persistence
  and before any egress.
- Redaction removes OTP-shaped codes, API-key-shaped tokens, Luhn-valid card numbers and
  password-labelled values, replacing each with `[REDACTED:<kind>]`.
- **No body text is ever written to the database.** Full bodies are fetched in memory only for unsure
  threads and discarded after the call.
- Ingestion is idempotent: re-ingesting a thread updates the existing row keyed by
  `(user_id, external_thread_id)`.
- The channel is accessed only through the `ChannelAdapter` interface — no Gmail-specific type crosses
  into the triage core.

## Success Criteria
- [ ] A run over a real mailbox with `limit=200` produces up to 200 `items` rows with real subjects and
      senders.
- [ ] Every `items` row's `snippet_redacted` is ≤ 200 characters.
- [ ] An automated test scanning every table confirms no column holds email body text.
- [ ] A seeded thread containing an API key and a card number stores and transmits `[REDACTED:api_key]`
      and `[REDACTED:card]` instead of the values.
- [ ] Re-running ingestion over the same mailbox creates zero duplicate `items` rows.
- [ ] A thread with 5 messages produces exactly 1 `Item` with `message_count = 5`.
