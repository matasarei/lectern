# M1b acceptance: the script sandbox and the coding model

The blueprint's M1 test for exact answers: 20 calculation prompts, and none may be answered without `code.run`. `lectern dev agent-check --set calc` sends each prompt to a fresh chat and scores it on two things: did the chat use `code.run`, and does the answer contain the expected numbers. The prompts and the expected values are in `src/lectern/dev/prompts/calc.yaml`; the expected values were computed with Python.

## How to run it

```sh
uv sync && npm --prefix web ci && npm --prefix web run build
uv run lectern serve        # needs Docker or Podman running
# in a second terminal:
uv run lectern dev agent-check --set calc
```

The first calculation asks two things with cards: whether to download the coding model, and whether to build the sandbox image. `agent-check` answers both with Allow (`--no-allow-cards` answers Don't allow instead). Each run leaves an "agent-check calc" project behind, so the scripts and the chats can be looked at afterwards.

## Results

### MacBook Pro, Apple M4 Max, 48 GB, macOS 27.0 · Docker 29.4.3 · llama.cpp b11236

Run on 2026-09-29 with `LECTERN_TIER=minimum`: the main model is Qwen3.5-4B Q4_K_M, the coding model Qwen2.5-Coder-1.5B Q4_K_M. These are the smallest models Lectern ships.

**First use.** The first prompt went through both cards, and passed in 151.6 s. That time covers:
- downloading the coding model (1.1 GB);
- building `lectern-sandbox:1` (429 MB);
- writing, testing and running the script.

**Run 1: 11/20 passed.**
- `code.run` was used in 14/20.
- The other 6 answers came from `code.write`'s result, which included the test run's output. Those numbers still came from a script, but not through `code.run`.
- Fixed: `code.write` now says only that the test passed and asks for `code.run`, which returns the stored test run at once.

**Run 2, after the fix: 16/20 passed.**

| Prompt | Tools | Answer | Expected | |
|---|---|---|---|---|
| percent | write, run | 409.5 | 409.5 | pass |
| sum | write, run | 4172.04 | 4172.04 | pass |
| days-between | write, run | 598 days | 598 | pass |
| penalty | write, run | 62.50 UAH | 62.50 | pass |
| compound | write, run | 14,795.11 | 14795.11 | pass |
| miles | write, run | 115.87 km | 115.87 | pass |
| weekday | write, run | Friday | Saturday | **wrong** |
| mean | write, run | 23.5 | 23.5 | pass |
| median | write, run | 23 | 23 | pass |
| stdev | write, run | 2.0 | 2 | pass |
| increase | write, run | 15% | 15 | pass |
| workdays | write, run | 23 | 23 | pass |
| durations | write, run | 7 h 26 min | 9 h 25 min | **wrong** |
| vat | write, run | 300.00 | 250 | **wrong** |
| ratio | write, run | 300 / 500 / 200 | 300 / 500 / 200 | pass |
| power | write, run | 1,099,511,627,776 | same | pass |
| csv-sum | write ×4, find, read, run | 1313.25 | 1313.25 | pass |
| csv-mean | write, run | 262.65 | 262.65 | pass |
| csv-count | find, read, write ×6 | none: hit the 8-call limit | 3 | **no answer** |
| xlsx-sum | write ×2, run | 39.25 | 39.25 | pass |

- **The criterion is met:** no calculation was answered without `code.run`. 19 of 20 used it. The one that didn't gave no answer at all: it ran out of model calls and said so.
- **Right answers:** 16 of 20.
- **Time:** about 2 to 4 s per prompt once both models and the image are in place. The longest were 15 s (csv-sum) and 38 s (csv-count), which needed several tries at the script.

### Where the wrong answers came from

The scripts in each run's project show which model made each mistake:
- **weekday:** the script was right. It printed `weekday()` = 5, which is Saturday when Monday is 0. The main model read 5 as Friday. A script that prints the day's name avoids this; the coder prompt could ask for readable output.
- **durations:** the coding model's mistake. Its script added 3:47 to 3:47 and never used 5:38 (seen in run 1; run 2 gave a different wrong total).
- **vat:** the main model's brief gave the wrong formula: 20% of the gross price, where the VAT included in it is gross − gross/1.2. The coder did what it was told.
- **csv-count:** the scripts kept failing their test run and the turn hit the 8-call limit. In run 1 the same prompt passed.
- **In run 1, the penalty** was wrong because the brief left out that it was April's rent. In run 2 the brief kept it, and the answer was right.

**So there are three kinds of mistake:**
- a brief that loses or distorts a fact (main model, 4B);
- a script with wrong logic (coder, 1.5B);
- reading the output wrongly (main model).

The test run only catches scripts that crash. Bigger models on the recommended and comfortable tiers should make fewer of these mistakes; that's the next thing to measure. Tighter coder rules (print labelled, human-readable results) would help both models.

## Notes

- **Real-engine tests** (`pytest -m sandbox` with `LECTERN_TEST_SANDBOX_IMAGE=python:3-slim`) pass on this Mac:
  - the container runs as uid 65534 and the network is blocked;
  - `/job`, `/in` and `/usr` are read-only, and the input file is left untouched;
  - `/out` is collected;
  - a run is killed at its time limit.
- **Not checked yet:** rootless Podman, where the Linux server has Podman. MLX as either model.
