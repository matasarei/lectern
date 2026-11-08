# Translation acceptance

Translation adds a `translate` role, a language's own model picked on **Settings ›
Languages** (see [Models and memory](../models.md#languages)), and the chat's `translate` tool
(see [The chat](../agent.md#translating)). Two things are measured:
- **Part 1:** how well each model translates each language, which decides what the Languages
  page offers.
- **Part 2:** whether the 4B calls `translate` when asked for a translation, and only then.

## Part 1: translation quality

### How to run it

`lectern dev translate-check` translates sentences into and out of English with each model,
through the same requests the tool sends (the translation model's own prompt, or the chat
model's), at temperature 0, and scores them with chrF (sacrebleu). The sentences were
FLORES-200 devtest (CC-BY-SA-4.0), the first 50 of each language, from the public archive,
kept in a scratch folder and not in the repository:

```sh
S=<a scratch folder>; D="$HOME/Library/Application Support/Lectern"
curl -L -o $S/flores.tgz https://dl.fbaipublicfiles.com/nllb/flores200_dataset.tar.gz
mkdir $S/flores && tar xzf $S/flores.tgz -C $S/flores
# en.txt, uk.txt, …: one sentence per line, copied from flores200_dataset/devtest/<code>.devtest
mkdir -p $S/home/models && ln -s "$D/models/qwen3.5-4b" $S/home/models/ && ln -s "$D/runners" $S/home/
LECTERN_HOME=$S/home LECTERN_TIER=minimum uv run lectern dev translate-check $S/sentences \
  --model salamandrata-2b --model hy-mt2-1.8b --model qwen3.5-4b --sentences 50 \
  --language uk --language ru --language pl --language cs --language de --language fr \
  --language es --language it --language ja --language zh
```

It writes `src/lectern/catalog/translation_scores.yaml` as it goes, a model and language at a
time, and skips pairs already there unless `--again` is passed. The models it needs download
first, through Lectern's own manager into the scratch home.

### Results

#### MacBook Pro, Apple M4 Max, 48 GB, macOS 27.0 · llama.cpp b11236

Run on 2026-11-08. The scores depend on the model and its quant, not on the computer: the
tiers decide only which models a computer is offered. Each cell is chrF into the language
(from English) / out of it (into English), and their mean, which the page compares.

**Minimum tier (4 GB):** the two small translation models against Qwen3.5-4B, the chat model.

| Language | SalamandraTA 2B | Hy-MT2 1.8B | Qwen3.5-4B |
|---|---|---|---|
| uk | 56.0 / 63.2 (59.6) | 54.3 / 63.9 (59.1) | 54.0 / 66.1 (60.0) |
| ru | 58.5 / 60.4 (59.5) | 56.4 / 61.1 (58.8) | 58.9 / 64.1 (61.5) |
| pl | 53.9 / 59.6 (56.8) | 52.7 / 59.3 (56.0) | 51.1 / 61.6 (56.4) |
| cs | 55.7 / 65.2 (60.5) | 58.5 / 63.8 (61.1) | 53.7 / 65.9 (59.8) |
| de | 62.4 / 67.7 (65.0) | 60.1 / 66.1 (63.1) | 61.6 / 69.5 (65.5) |
| fr | 72.0 / 67.5 (69.8) | 68.5 / 67.2 (67.8) | 70.7 / 70.8 (70.8) |
| es | 58.9 / 61.6 (60.2) | 56.4 / 60.9 (58.6) | 56.5 / 60.7 (58.6) |
| it | 62.6 / 61.3 (62.0) | 58.1 / 59.6 (58.9) | 59.9 / 62.2 (61.0) |
| ja | — | 43.1 / 56.4 (49.8) | 40.1 / 58.2 (49.2) |
| zh | — | 39.6 / 59.6 (49.6) | 39.7 / 60.4 (50.0) |

What it means:
- On a 4 GB card the chat model translates about as well as the small translation models.
  Most gaps in the mean are under a point, and the chat model leads in Ukrainian, Russian,
  German and French. With an option required to beat the chat model by a point
  (`MARGIN`), a 4 GB card is offered two: Czech (Hy-MT2 1.8B) and Spanish (SalamandraTA 2B).
- The direction matters. The translation models write the other language better (into
  Ukrainian 56.0 against 54.0, Czech 58.5 against 53.7, Polish 53.9 against 51.1); the chat
  model writes English better (out of Ukrainian 66.1 against 63.2). The mean hides that; a
  translation model used only for the direction it wins would do better than either.
- SalamandraTA 2B didn't stop on 2 of the 1,000 sentences (one French, one Russian): it
  repeated itself until it ran out of room. The tool then hands the text to the chat model.

**Recommended tier (8 GB):** Hy-MT2 7B and SalamandraTA 7B against Qwen3.5-9B. See below
when the run is done.

**Comfortable tier:** not measured. Its chat model (Qwen3.5-35B-A3B) has no scores, so a
comfortable computer is offered nothing until it is.

## Part 2: does the 4B call `translate`?

### How to run it

`lectern dev agent-check --set translate` against a scratch server on the minimum tier, with
the 4B: ten requests for a translation, in Ukrainian, English and Russian, two of them for a
file, which must call `translate` with the right languages; and three that mustn't (what a
word means, a question answered in Ukrainian, which language a phrase is in). Each runs in a
fresh chat. Run each wording at least twice: the 4B varies between runs.

### Results

#### MacBook Pro, Apple M4 Max, 48 GB · llama.cpp b11236 · Qwen3.5-4B Q4_K_M

The rule is in the tool's description only: the system prompt is at its length budget.

| Run | Wording | Translations calling `translate` | Controls without it |
|---|---|---|---|
| 1 | "Use it whenever the person asks for a translation; don't translate yourself." | 7/10 | 3/3 |
| 2 | + "or what something says in another language"; `source` is the text's or file's language, "not the person's" | 9/10 | 3/3 |
| 3 | same as 2 | 9/10 | 3/3 |

In run 1 the 4B called Russian text `uk`, gave a file's source as the person's language
(`uk` to `uk`, then rewrote the file by hand when that was refused), and translated "What does
this say in English? «…»" itself. The tool now tells Ukrainian and Russian apart by their
letters, its `source` field says whose language it is, and the refusal says what to give
instead. "What does this say in English?" is still answered without the tool in every run:
it reads like asking what a word means, which mustn't call it.

## Part 3: the page and the chat in a browser

Chrome on the MacBook Pro above, a scratch server on the minimum tier (2026-11-08):
- **Settings › Languages**, opened from Models & memory's header: Czech and Spanish in "Add a
  language"; the languages measured with no better model, and those not tested yet, each in a
  line under it. (The first version said every language without an option was as good on the
  chat model, the untested ones too: fixed.)
- **Adding Spanish**: a new row on SalamandraTA 2B (its only option, so no dropdown), with
  its help (best results, 60.2 against the chat model's 58.6, open licence). Nothing was sent
  before **Download**; after it, the row said Downloaded (the model was on disk) with Remove.
- **At 390 px wide**: nothing wider than the page; the dropdown capped at 192 px; the table
  inside its card.
- **The chat**: "Traduce al inglés: …" was translated by SalamandraTA 2B
  (`translate@salamandrata-2b` among the runtime's residents) and its translation was the
  reply as it stands. "Translate into Czech: …" was translated by the chat model, with
  "Czech gets better translations from a model made for translation. Add Czech in Languages."
  under the tool's line; after reloading the page it was still there.

Not checked in the browser: a restricted model's licence acceptance and changing a model
(no restricted option and no second option on the minimum tier with these scores; both are
in the page's tests), download progress (the models were on disk), and a member's view
(in the page's tests and the API's).

