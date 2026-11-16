# Voice acceptance

Replies read aloud by a Piper voice on the CPU (see [Models and memory](../models.md#voices) and
[The chat](../agent.md#reading-replies-aloud)). What was run, where, and what is still open.

## macOS, M4 Max (2026-11-16)

A scratch server on port 8796 from the `feature/voice-reader` worktree, its home in a scratch
folder with only the 4B chat model and the runners linked from the developer's install, so the
voices downloaded into the scratch folder:

```sh
S=<a scratch folder>; D="$HOME/Library/Application Support/Lectern"
mkdir -p $S/home/models && ln -s "$D/models/qwen3.5-4b" $S/home/models/ && ln -s "$D/runners" $S/home/
npm --prefix web run build
LECTERN_HOME=$S/home uv run lectern serve --port 8796 --no-browser
```

The chat ran on the 4B (llama-server, Metal), its replies real.

- **Russian, from the button.** A two-sentence Russian reply: **Read aloud** found `ru`, and with
  no Russian voice on disk showed "Download the Russian voice (63.2 MB)" under the reply. Its
  download finished and passed its SHA-256; **Read aloud** then spoke both sentences, the button
  going Getting the voice ready… → Stop reading → Read aloud.
- **English with a table.** Two sentences and a two-row table: `en`, the English voice downloaded
  the same way, then five sentences spoken (the two, the header row and two rows), each fetched
  while the one before it played, over 13 s.
- **The chat model stayed.** During and after reading, `GET /api/runtime` listed the 4B loaded,
  0 swaps, nothing reserved. The server log had no warning.
- **Read replies aloud.** Switched on, a Russian question sent: when its turn ended the reply was
  found as `ru` and spoken with no click. Reloading the page, the toggle still on, read nothing:
  the replayed turn isn't read.
- **More languages, through the app.** Portuguese (Tugão, whose file path has "ã") and Ukrainian
  (Ukrainian TTS, three speakers) downloaded and checked, then `POST /api/voice/language` and
  `/speak`:

  | Sentence | Found | Speech | Request, first load included |
  |---|---|---|---|
  | "O contrato termina a 3 de maio de 2027." | `pt` | 2.7 s | 0.20 s |
  | "Договір закінчується третього травня…" | `uk` | 4.1 s | 0.20 s |
  | "Договор заканчивается третьего мая." | `ru` | 2.7 s | 0.17 s |
  | A 160-character Russian sentence | | 9.6 s | 0.28 s |

  Finding the language of a 1,800-character German reply took 2 ms once py3langid's model was
  loaded.
- **Voices card.** All 23 languages by name, the four downloaded marked On this computer.
  Choosing Russian Ruslan showed its licence and note first; **Keep the voice it has** sent
  nothing.
- **piper-tts on its own** (step 1, before any app code): Russian Denis loaded in 0.11 s and spoke
  9.1 s of speech in 0.25 s.

I couldn't listen to the sound in these runs. The button's states and the requests, spaced as
long as each sentence played (Web Audio's `ended`), are the evidence that playback ran.

## Not run yet

- **The 4 GB PC and the 8 GB PC** (CUDA, Windows and Linux): how long the first sentence takes
  on their CPUs, reading while the chat model runs on the CPU (below the minimum tier), and
  playback in their browsers. CI runs the tests on Windows; nothing here has spoken there.
- **Listening.** Whether the default voices sound right to a speaker of each language, numbers
  and dates included (espeak-ng reads them per language), and Russian stress.
- **Safari and Firefox.** Only Chrome was used.
