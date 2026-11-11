# Models and memory

Lectern runs every model on the machine it's installed on, each in a runner process of its
own, and decides which ones stay in memory. On a small GPU, swapping models is the normal way
it works. The code is in `src/lectern/hardware/`, `catalog/`, `runtime/`, `runners/` and
`downloads/`.

## Roles

A module asks for a **role**, not a model:

| Role | What it does |
|---|---|
| `main` | the chat |
| `ocr` | reads printed pages |
| `ocr.hand` | reads pages marked handwritten |
| `code` | writes the scripts the chat runs in the [sandbox](agent.md#the-script-sandbox) |
| `image` | makes pictures for the chat's [`image.generate`](agent.md#making-pictures) |
| `translate` | translates to and from the languages added on the Languages page ([below](#languages)) |

Cleaning up OCR pages has a choice of its own, `clean`: the chat's model unless another is
picked. When the chat runs on MLX, cleanup uses the minimum tier's GGUF chat model, since it
needs llama-server's JSON-schema output.

An admin picks a model for each role on the **Models & memory** page, except `translate`,
whose model is picked per language. Any model the catalog
lists for that role can be chosen; one meant for a bigger tier is marked as more than this
computer is meant for. A change applies from the next reply.

## Hardware tiers

The hardware is probed at every start (`hardware/probe.py`): NVIDIA cards through NVML, AMD
and Intel cards through the Vulkan build of llama-server, Apple Silicon through `sysctl`, RAM
through psutil. The tier comes from the largest GPU (`hardware/tiers.py`):

| Tier | PC: GPU memory | Mac: GPU budget | Chat model | Chat context |
|---|---|---|---|---|
| Comfortable | ≥ 15 GiB | ≥ 20 GiB (32 GB Macs and up) | Qwen3.5-35B-A3B | 32k |
| Recommended | ≥ 7.5 GiB | ≥ 10 GiB (16 GB Macs) | Qwen3.5-9B | 16k |
| Minimum | ≥ 3.5 GiB | ≥ 5 GiB (8 GB Macs) | Qwen3.5-4B | 8k |
| Below minimum | no usable GPU | | Qwen3.5-4B on the CPU | 8k |

A PC with less than 14 GiB of RAM is held at the minimum tier. `LECTERN_TIER` overrides the
detected tier, for testing a small tier on a big machine. When the tier changes, the
*recommended* marks move, and a role without a chosen model keeps the largest suitable model
already on disk.

## The catalog

`src/lectern/catalog/models.yaml` lists every model Lectern can download: its Hugging Face
repository and the exact revision, each file's size and SHA-256, the licence, the roles, the
context per tier and which tiers it is the default for. Defaults:

| Role | Below minimum / Minimum | Recommended | Comfortable |
|---|---|---|---|
| `main` | Qwen3.5-4B Q4_K_M (2.7 GB) | Qwen3.5-9B Q4_K_M (5.7 GB) | Qwen3.5-35B-A3B Q4_K_M (22 GB) |
| `ocr` | PaddleOCR-VL-1.6 F16 (1.8 GB) | same | same, 8k context |
| `ocr.hand` | Qwen3-VL-4B Q4_K_M (3.0 GB) | Qwen3-VL-8B Q4_K_M (5.8 GB) | same, 8k context |
| `code` | Qwen2.5-Coder-1.5B Q4_K_M (1.1 GB) | Qwen2.5-Coder-7B (4.7 GB) | Qwen2.5-Coder-14B (9.0 GB) |
| `image` | FLUX.2 klein 4B Q4_0 (5.2 GB) | FLUX.2 klein 4B Q8_0 (7.0 GB) | same |

Sizes include the vision models' projector files, and an image model's text encoder and VAE. Every default has a permissive licence
(Apache-2.0); the catalog refuses to load otherwise. Two models with restricted licences can
be chosen but are never a default: Chandra OCR 2 for handwriting and Qwen2.5-Coder-3B. The
licence is shown, and has to be accepted, before any download. The catalog also has MLX
builds of the three Qwen3.5 chat models (see [MLX](#mlx)), and Z-Image-Turbo (Q4_K, 6.7 GB) as
a second image model to choose.

An image model is three pieces, often from three repositories, so each of its files can name
its own repository and pinned revision: the diffusion model, the text encoder (Qwen3-4B) and
the VAE. Its version on disk is a digest of all three revisions, so a newer pin of any piece
shows as an update. Its `image:` block gives the steps, the guidance and the sampler, and for
each tier the default square and the most pixels a picture may have.

## Languages

A translation model is a language's own. On **Settings › Languages** (linked from Models &
memory) an admin adds a language and picks its model; the download starts then. Translating
to or from an added language uses its model when that model translates the other language
too, the target's first; anything else is translated by the chat model
(`modules/translate/languages.py`, `choose`). Members see the page and change nothing.

A language is offered only the models that suit this computer's tier and were measured
translating it better than the chat model this computer runs, by a point at least: chrF,
into and out of English, on 50 sentences of FLORES-200 devtest, in
`src/lectern/catalog/translation_scores.yaml` (written by `lectern dev translate-check`,
results in [`acceptance/translate.md`](acceptance/translate.md)). A model nobody measured isn't
offered, nor is anything against a chat model nobody measured. The page lists the languages
where none beats the chat model, and the ones not measured yet. On a 4 GB card the chat model
is about as good as the small translation models: two languages are offered there, Czech and
Spanish. On an 8 GB card SalamandraTA 7B (restricted licence) beats the 9B in nine of the ten
measured languages, all but Chinese; with an open licence, Hy-MT2 7B beats it in Czech, German
and Japanese. Each option is marked as the best results, the best with an open licence, or with a
restricted licence, and its help says what that means and how it scored. A new language starts
on the open-licence option, so a restricted one is only ever taken with a click, and its
licence is accepted before the download, as on Models & memory.

A translation model is never a default, whatever its licence (the catalog refuses one). Its
`translate:` block lists its languages and the prompt it was trained on (`{source}`,
`{target}`, `{text}`). The catalog has SalamandraTA 2B (Apache-2.0, 35 European languages),
Hy-MT2 1.8B and 7B (Apache-2.0, 33 languages from Arabic to Ukrainian), and SalamandraTA 7B v3
(GPL-3.0, 40 languages). The 1.8B and the 2B suit every tier, the 7B models the recommended and
comfortable ones. Changing a language's model keeps the one it had translating until the new
one is on disk; removing a language leaves its model on disk, for Models & memory to remove.
Translation models run on llama-server, MLX or not.

## Downloads

Models download from Hugging Face at their pinned revision, with resumable `.part` files,
each file checked against its SHA-256, and 1 GiB of disk always left free
(`downloads/manager.py`). The first run downloads only the chat model and the llama.cpp
build; the OCR and coding models download the first time they're needed, after asking. When a
new Lectern pins a newer revision, the installed model shows **Update**: the new files
download beside the old ones, which stay in use until the model is free. Models live in
`<data>/models/`, a folder each.

The first-run page shows each download's bytes and speed. The llama.cpp build's progress
comes every 2 % of it or every second while bytes arrive (`runners/llamacpp.py`
`install_reporting`); after 30 s with none, its row says the download seems stuck, and after
120 s without any it fails (the read timeout) and can be tried again.

## Runners

Each model runs in its own process on `127.0.0.1`, on a free port, with an API key in a file
only the owner can read (`runners/`). Unloading a model ends its process, so the memory is
certain to come back, and a crashed runner is started again on the next request.

- **llama-server** (llama.cpp, build pinned in `runners/binaries.py` with its size and
  SHA-256) runs everywhere. The build is chosen from the machine: CUDA 13 when the driver and
  card support it, else CUDA 12, else Vulkan when a Vulkan loader is present, else CPU; Metal
  on a Mac. It is started with `--jinja`; OCR uses its log-probabilities and cleanup its
  JSON-schema output.
- **mlx_lm.server** runs the chat model on Apple Silicon (see below).
- **sd-cli** (stable-diffusion.cpp) makes pictures: see [Image models](#image-models).

### MLX

On a Mac with Apple Silicon and mlx-lm installed (the Mac installer adds it), an admin can
turn MLX on under Models & memory. The chat model then runs from its MLX build once that's
downloaded; until then the GGUF build keeps running. MLX covers the `main` role only: OCR,
handwriting, cleanup and code stay on llama-server.

### Image models

An image model doesn't stay loaded. stable-diffusion.cpp's `sd-cli` runs once per picture and
exits (`modules/image/generate.py`). It has no port, so there's no key to keep, and its memory
comes back when it ends. The release also has a server, `sd-server`, but it has no API key and
isn't used.

The build is pinned apart from llama.cpp's (`runners/sdcpp.py`, with each asset's size and
SHA-256) and downloads the first time a picture is made. It is chosen from the machine:
- Metal on a Mac;
- CUDA 12 for NVIDIA on Windows;
- Vulkan where a loader is present, which includes NVIDIA on Linux, since there's no Linux CUDA
  build;
- else CPU.

The Mac build needs macOS 26 or later.

Every tier gets the same pipeline; a bigger GPU gets the 8-bit build and larger pictures:

| Tier | Default picture | Largest | How it runs |
|---|---|---|---|
| Below minimum | 512² | 768² | on the CPU |
| Minimum | 768² | 1024² | weights wait in RAM and come to the GPU part by part; the VAE decodes in tiles |
| Recommended | 1024² | about 1.5 MP | weights wait in RAM and come to the GPU part by part |
| Comfortable | 1024² | about 2 MP | everything on the GPU |

These are starting values until measured on 4 GB and 8 GB machines
([acceptance](acceptance/image.md)). The room a picture needs is estimated as:
- the largest piece when the weights wait in RAM, or all three pieces when they don't;
- plus 1 GiB per megapixel and 300 MiB.

On the minimum and recommended tiers `sd-cli` is told the same figure (`--max-vram`).

## Fitting a model

There is one memory budget (`runtime/wiring.py`): `LECTERN_BUDGET_MB` if set, else the
largest GPU's memory less 0.6 GiB, or on a Mac the GPU's share of unified memory (⅔ of RAM up
to 36 GB, ¾ above, or `iogpu.wired_limit_mb` when it's set). With no GPU it is RAM less
4 GiB.

A model's need is estimated as its file sizes, plus the KV cache for the context (from the
model's header, read by `runtime/gguf_header.py`, which skips the tokenizer's arrays: gguf's
own reader took 9 s over them on a 248k-token vocabulary), plus 300 MiB (`runtime/planner.py`). The planner tries, in order:

1. the tier's context with an f16 KV cache;
2. the same context with a q8_0 KV cache;
3. half the context, then half again, down to 4k, with q8_0.

Next to a model already loaded only the first two are tried: the context is never shortened
to share the GPU, the runtime swaps instead. If even 4k doesn't fit on its own, llama.cpp
puts layers in RAM (`--fit`), unless that would bring a chat below about 8 tokens a second;
then the chat model is refused with a message to choose a smaller one. Once loaded, the
model's real size is read from the runner's log and counted against the budget.

## Leases and swapping

Work takes a **lease** on a role for as long as it needs the model (`runtime/supervisor.py`).
If the model isn't loaded and doesn't fit, idle models are unloaded, least recently used
first; if memory is still short, the lease waits. Chat requests go ahead of jobs. A job whose
lease would unload the chat model waits until the chat has been idle for two minutes, unless
**Start now** is pressed; an OCR job gives its lease back after every page, so a chat gets in
between pages.

An integrated GPU on a PC has no memory of its own: what it uses is system RAM, shared with
everything else. llama.cpp's Vulkan build says which GPU is which (`uma: 1` in what
`llama-server -v --list-devices` logs, `hardware/vulkan.py`); nothing else is reliable, as an
8 GB card in a 16 GB PC has the same share of RAM as an integrated GPU allowed half of it. If a
discrete GPU is listed too, the budget and the tier come from it. On an integrated GPU alone the
tier is what its memory gives, as for a card, but a model must also fit what the system can
spare at that moment: RAM free now, plus what the loaded models hold, less 2 GB. The planner
then tries a shorter context, and the runtime unloads idle models or waits for one in use, as it
does for the budget. A model that doesn't fit even alone at the smallest context is refused
whatever its role, with what it needs, what can be spared, and to close some apps, since putting
layers on the CPU would keep them in the same RAM. A model over the GPU's share but within what
can be spared does spill. None of this has been run on a real integrated GPU yet
([`acceptance/m4.md`](acceptance/m4.md)).

On Windows a model that fits the GPU can still run out the commit (RAM and page file
together): Windows backs GPU memory with it, and a llama-server commits about 1.5 times the
planner's estimate (measured: PaddleOCR-VL 3.25 GB of an estimated 2.06, the 9B 8.28 of 6.08).
So before a model loads beside others, the runtime reads the commit left
(`GlobalMemoryStatusEx`); when the model would leave less than 2 GB, an idle model is unloaded
first, least recently used first, counted as a swap, and the log says why. With everything
loaded in use it loads anyway, as before. On an 8 GB card that is the OCR reader, unloaded
when cleanup's model loads on a PC already short of commit. On such a PC a chat during the
reading also swaps the reader and the chat model, which stay loaded side by side elsewhere; the
two-minute wait above keeps that to a swap there and back per chat, not per message.

A picture holds room without a runner (`reserve` in `runtime/supervisor.py`). It waits in the
same queue as a lease, by the same rules:
- idle models are unloaded to make room, least recently used first, each counted as a swap;
- a model in use is never unloaded;
- the room is given back however the picture ends.

The picture is part of the chat's reply, so it reserves at the chat's priority, as the coding
model leases. It is never held for the chat's quiet time, and the chat's model, idle while
the tool runs, can be unloaded for it. A chat model that doesn't fit beside a picture being
made waits for it to finish, or for Stop. On a 4 GB or 8 GB card a reply that makes a picture
runs main → image → main. The runtime's state lists the room held under `reserved`.

The Models & memory page shows the budget, what's loaded, the number of swaps and what's
waiting, and can unload everything. Runtime events stream to the UI over
`/api/runtime/events`.

## Checking a machine

- `lectern doctor` prints what Lectern knows about the machine as JSON; `--runners` adds the
  llama.cpp build it would use and whether it's installed.
- `lectern bench` measures chat speed and the first token after a load against the tier's
  targets. Results for real machines are in [`acceptance/m0.md`](acceptance/m0.md) and
  [`acceptance/m0-windows-8gb-results.md`](acceptance/m0-windows-8gb-results.md).
- `lectern dev swap-test` runs an OCR-shaped workload and counts the swaps.
