# A tool's unexpected exception kills the chat turn silently: "Working…" for good

**Status (2026-10-29):** built on branch `fix/turn-dies-silently`, not committed. Differs from
the design below: the catch is in `run_turn` (`agent/loop.py`), beside its other handlers, not in
`turns.py`'s `turn()` — both the chat turn and the follow-up path call `run_turn`, so one place
covers both; `stop()` is unchanged (a turn now always publishes its end). Proven with the real
case on Windows (`lectern.cmd serve`, "₴"): the error shown, the same after a reload, the next
message answered. **Follow-up found:** an error isn't stored in the chat's history (that's how
every failed turn already works), so the model doesn't know the last turn failed — asked "what
went wrong", the 9B said the script "wasn't run yet". Worth storing a short note of the failure
as a message the model sees. **Done on the branch** (the review's nit): the failed call gets
`Error: <type>: <reason>` as its result; the turn still ends.

**Type:** bug
**Language:** en
**Asked:** "write all issues to the tasks to fix them" (M4 Windows run, 2026-10-29)
**Scope:** all platforms; triggered on Windows by the encoding bug (text-encoding-utf8.md)

## Summary
- When a tool raises anything but `ToolError` (here `UnicodeEncodeError` from `code_write`), the
  turn's task ends with the exception stored and unreported: the chat shows "Working…" with only
  Stop, also after a reload; Stop does nothing; no new message can be sent; nothing is logged.
- Fix: the turn always ends visibly. An unexpected exception is logged with its traceback and
  becomes a failed turn the person sees; the chat can be used again.

## Cause
- `src/lectern/agent/turns.py:192-204`: `turn()` publishes `{"type": "turn", "status": …}` only
  after `run_turn` returns; an exception skips it. The task stays in `self._turns`, so asyncio
  never even logs "Task exception was never retrieved".
- `turns.py:472-479`: `stop()` returns at once when the task is done; nothing is published.
- `agent/loop.py:351, 390, 399`: tool calls catch `ToolError` only. `modules/sandbox/tools.py:382`
  catches `NotAvailable, RunnerError`.
- The reloaded page replays the channel's events, which end at "running".

## Design
In `turn()`: `except Exception` around `run_turn` (not `BaseException`: cancellation stays
Stop's) → `log.exception(...)`, status `"failed"` with a short reason ("Something went wrong
running code_write: UnicodeEncodeError. It's in the log."), published like any other end, then
`settle()`. `stop()` on a finished task publishes the end if the channel never got one.
Separately, a tool's unexpected exception could become a tool error the model sees (as
`turns.py:440` already does for follow-ups) — decide: one turn failing visibly is enough at first.

## Acceptance criteria
- [ ] A tool raising `ValueError` mid-turn: the chat shows the turn failed with the reason; the
      log has the traceback; a new message can be sent.
- [ ] A reload after that shows the failed turn, not "Working…".
- [ ] Stop on a running turn behaves as now.

## Steps
1. Catch, log and publish in `turn()`; `stop()` on a finished task closes the channel.
   - Modify: `src/lectern/agent/turns.py:192-204, 472-479`
   - Test: `tests/agent/test_turns.py` (a fake tool raising): status "failed", event published,
     log record, next `send` accepted
2. The UI shows a failed turn's reason (if it doesn't already) — check `web/src` turn status
   handling; i18n strings in `web/src/locales/`.
3. Docs: `docs/agent.md` where turns end.

## How to check it
- `uv run pytest tests/agent -q && npm --prefix web test`

## Do not touch
- Cancellation semantics (Stop), the follow-up path (`turns.py:440`).

## Evidence
- **Data:** chat "Порахуй 17% від 2 409,50 грн… «Сума: 409,62 ₴»": coder answered once (99 tokens,
  17:23), no "Test run in the sandbox" progress, then "Working…" for 20+ min; `POST …/stop` 200,
  UI unchanged; a reload replayed "Working…"; the composer offered only Stop.
