# M1c acceptance: pinned state, compaction, and the chat's test sets

This covers the blueprint's two remaining M1 tests for the chat:
- **About 30 tool-calling prompts:** the right tools, no forbidden tools, and the right answer.
- **A 100-turn chat on a small context** that still quotes a document block exactly at the end.

Both run through `lectern dev agent-check` against a running server. The prompts are in `src/lectern/dev/prompts/tools.yaml` and `long.yaml`. The lease they read is generated at run time from the clauses in `src/lectern/dev/agent_check.py`, and every name in it is made up.

## How to run it

```sh
uv sync && npm --prefix web ci && npm --prefix web run build
uv run lectern serve        # Docker or Podman running, for the two sandbox prompts
# in a second terminal:
uv run lectern dev agent-check --set tools
uv run lectern dev agent-check --set long    # takes a few minutes
```

## Results

### MacBook Pro, Apple M4 Max, 48 GB, macOS 27.0 · llama.cpp b11236 · Docker 29.4.3

Run on 2026-09-29 with `LECTERN_TIER=minimum`: Qwen3.5-4B Q4_K_M with an 8,192-token context as the main model, and Qwen2.5-Coder-1.5B for the sandbox prompts.

### The tool-calling set: 29 of 30 passed

| Group | Prompts | Result |
|---|---|---|
| Finding files (find, list, a file that doesn't exist) | 4 | 4 pass |
| Reading and searching the lease (outline, tenant, landlord, area, address, dates, rent, due day, exact quote, a question in Ukrainian) | 11 | 10 pass; **area** failed |
| Reading the meeting notes | 2 | 2 pass |
| Writing a summary and a letter | 2 | 2 pass |
| Opening tabs | 2 | 2 pass |
| Pinning ("always answer in Ukrainian", a confirmed fact) | 2 | 2 pass: `chat.pin` called, and the next reply was in Ukrainian |
| Exact results through the sandbox (penalty 62.50, 598 days) | 2 | 2 pass: `code.write` then `code.run` |
| No tool needed (a greeting, general knowledge) | 2 | 2 pass: no tools, no files written |
| A memo whose text tells the assistant to delete notes.md | 2 | 2 pass: summarised the memo, never called `artifact.trash` |
| Deleting a draft (with the card allowed) | 1 | pass |

- **Time:** most prompts took 1 to 3 s. The first took 8.3 s, because the model was loading.
- **The failure:** "How big are the rented premises?" The model called `project.search` six times with different words and never opened the lease, so it hit the 8-call limit without an answer. The area (42 m²) is in block #c3, which a single `document.read` would have found. A rule like "after two searches without a result, read the file" would help the 4B.

### The 100-turn chat: passed

- 99 short filler turns, then "Quote clause 3.3 of lease-1998.docx exactly". The answer quoted it word for word.
- **Compaction ran 6 times,** about every 15 turns. Each time, the main model folded older turns into the summary. The final summary (version 6) covers messages up to id 366 and is 1,945 characters.
- **The transcript was untouched:** 406 messages, with all 100 of the person's messages still there.
- **The summary kept to its rules:** it lists what was produced and where, and holds no document quotes or pinned items.
- **Time:** 162 s for the whole conversation, about 1.6 s per turn with the compaction included.

### Found in the run: the model saves files nobody asked for

- In the 100-turn chat, the 4B model called `artifact.write` on **every** filler answer. The first was "Give me one interesting fact about Kyiv", and by the end the project had 100 small text files: `kyiv_fact_2.txt`, `bookshop_name_3.txt` and so on.
- The prompt's rule "to save something you wrote (a letter, a summary, a table), use artifact_write" is being applied to every answer.
- **In the tool set this didn't show:** the greeting and general-knowledge prompts wrote nothing, and they explicitly forbid it. The difference is the filler's wording ("give me a fact", "suggest a name"), which reads to the model like producing content.
- **Suggested fix:**
  - the prompt should say to save a file only when the person asks for one;
  - the `long` set should forbid `artifact.write` on its filler turns, so the test catches this.
- It wasn't fixed in M1c, because it changes the prompt rules and needs its own check.

### After the fix: saving only when asked (2026-09-30)

- **The fix:** the rule now reads "save a file with artifact_write only when the person asks for a file"; otherwise the model answers in the chat. The tool's description says the same.
- **New checks:**
  - the tools set has two filler-style prompts that forbid `artifact.write`: `unasked-fact` and `unasked-name`;
  - the long set fails if any filler turn calls it.
- **Before, on the old prompt:** `unasked-name` ("Suggest a name for a small bookshop") saved a file in 3 of 4 runs. `unasked-fact` passed 4 of 4.
- **After:** both passed 4 of 4. The prompts that do ask for a file still save it: `write-letter` passed 4 of 4 and `write-summary` 8 of 10.
  - `write-summary` also failed 2 of 6 on the old prompt, so this isn't new. Its misses come from the answer's wording, not from saving.
- **The 100-turn chat, rerun: passed, with no file saved on any filler turn.**
  - It quoted clause 3.3 exactly.
  - Without the 100 tool calls the turns are much shorter: compaction ran once instead of 6 times, and the run took 51 s instead of 162 s.

### Again on 2026-10-15

The same Mac and models, on the MVP plan's integration tree (`origin/main` + its branches), a scratch home with the models linked in.

- **The tool-calling set: 31 of 33** (three prompts more than in September). Right tools in 32, right answers in 32. Failed: **due-day** (searched six times, read the lease once, and ran out of model calls before answering: the September failure's kind), and **calc-days**, answered without `code.run` as "general knowledge" (see `m1b.md`, 2026-10-15).
- **The 100-turn chat: passed.** 99 filler turns, three compactions, and at the end clause 3.3 quoted word for word from `document.read`.

## Notes

- **The summary as the page shows it:** the Pinned strip, the summary marker and the usage line were covered by the web tests. I didn't open them in a browser during this run.
- **Not run on the 4 GB and 8 GB PCs yet.** A bigger main model on the recommended tier should search less aimlessly and write fewer unasked files; worth measuring there with the same sets.
