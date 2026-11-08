# Image generation acceptance

Image generation adds an `image` role, run by stable-diffusion.cpp's `sd-cli` once per picture
(see [Models and memory](../models.md#image-models)), and the chat's `image.generate` tool
with its card and the Image toggle (see [The chat](../agent.md#making-pictures)). This file
has two parts:
- **Part 1:** false triggers. These are measured, below.
- **Part 2:** time and memory per picture on the 4 GB and 8 GB PCs and on a Mac. Not run yet:
  the tier sizes, the memory estimate and the first-guess waits in the code are starting
  values until it is.

## Part 1: false triggers on the 4B

How often the minimum tier's chat model calls `image.generate`:
- for messages that mustn't make a picture;
- for messages that should.

### How to run it

The model is asked with the turn's real system prompt and tool list: the core tools, the
export tools, and the modules' tools with no container engine and web search off. Each
message is asked twice: wording on the 4B varies between runs. The scratch home links
`models/` and `runners/` from the dev data and is only read:

```sh
S=<a scratch folder>; D="$HOME/Library/Application Support/Lectern"
mkdir -p $S/home && ln -s "$D/models" $S/home/models && ln -s "$D/runners" $S/home/runners
LECTERN_HOME=$S/home LECTERN_TIER=minimum uv run python false_triggers.py 2
```

The script asks each message as one completion with the tools, and counts the replies that
call `image_generate`. It was a scratch file and isn't in the repository. The message sets are
below.

### Results

#### MacBook Pro, Apple M4 Max, 48 GB, macOS 27.0 · llama.cpp b11236 · Qwen3.5-4B Q4_K_M

Run on 2026-11-07, at temperature 0.6, as the chat runs.

**Messages that mustn't make a picture: 0 of 40 called the tool** (20 messages, twice each).
- Questions about pictures: "What's in this picture?", "Describe the image I uploaded
  earlier.", "Find the images in this project.", "List the pictures in the images folder.",
  "What image formats can I import?", "Is there a figure in the OCR document?", "What does the
  diagram on page 3 show?", "Which files mention a photo?", "Rename the image file to
  cover.png.", "How much memory does making a picture need?", "Can you explain what an image
  model is?", "What's a good prompt for a picture of a lighthouse?"
- Figures of speech: "Picture this: we sign the lease on 3 May…", "Imagine a tenant pays 3
  days late…", "Paint me a picture of the payment terms in words.", "Draw a conclusion from
  the two contracts.", "Can you illustrate the difference between the two clauses with an
  example?"
- Other requests: "How do I export the document as DOCX?", "Summarise the lease in three
  points.", "Show me the file lease.md."

**Messages that should: 16 of 20 called the tool.** All of these did, both times: "Draw a
lighthouse at dusk.", "Make a picture of a cat on a roof.", "Generate an image of a mountain
lake.", "Make me a photo-like image of a red barn in snow.", "I need a picture of a robot
reading a book.", "Paint a watercolour of a forest.", "Can you make an image of a city at
night?", "Draw a logo with a lectern on it."

Two requests never called it, in either run:
- "Create an illustration for a book cover about the sea."
- "Make a cover image for a report about rent."

#### The same, with the description that says not to ask first

Run on 2026-11-08, on the same machine and model. On the "cover" requests the model had
answered in words asking for details. Typical replies: "To create the illustration, I need a
bit more detail…", or "Could you provide: 1. The style…". Asked again with the old
description, two more requests for a cover or title image did the same: 1 of 8 calls in all.

The description now says:
- use the tool for a cover or title image too;
- don't ask for details first: write a full prompt and call it at once, since the person sees
  the prompt on the card and can change it.

With that description, each message asked twice:

| Set | Called `image.generate` |
|---|---|
| Messages that only talk about pictures (the 20 above) | 0 of 40 |
| Requests for a picture (the 10 above) | 20 of 20 |
| Covers and title images: the two above, plus "Design a cover for my lease summary." and "I need a title image for the presentation about the budget." | 8 of 8 |

## Part 2: time and memory per picture

**Not run yet.** It needs the 4 GB and 8 GB PCs. The production host `archive` isn't used for
it: nothing there is stopped or replaced.

### How to run it

On each machine, with the default image model of its tier:

```sh
uv run lectern serve        # or a scratch server on another port, LECTERN_HOME of its own
```

Then, in a chat, with each shape (square, portrait, landscape, large):
1. Ask for a picture with the Image toggle on. The first time, the model and the engine
   download after a card.
2. Read `GET /api/runtime` before, during and after the picture: `reserved` holds the room it
   was given, and `residents` shows the chat model unloaded and loaded again.
3. Read the picture's timing:
   ```sh
   sqlite3 <home>/lectern.sqlite 'SELECT * FROM image_timings'
   ```
   That gives the load, the time per step and the decode.
4. Read the peak GPU memory: `nvidia-smi --query-gpu=memory.used --format=csv -lms 200` on a PC,
   and Activity Monitor's GPU memory on a Mac.
5. Time the chat's first token after the picture: the reload of the chat model.

What it should settle in `src/lectern/modules/image/generate.py` and the catalog:
- `COMPUTE_PER_MEGAPIXEL` and `OVERHEAD`, against the peak memory.
- `FIRST_GUESS_SECONDS` per tier, against the total time at the default size.
- The tier sizes in `models.yaml`: whether 768² on 4 GB and 1024² on 8 GB are right.
- Whether a GTX 1050 Ti (Pascal) runs the Vulkan build.
- Whether the Linux builds (made on Ubuntu 26.04) start on the installed Linux.
