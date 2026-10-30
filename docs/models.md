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

Cleaning up OCR pages has a choice of its own, `clean`: the chat's model unless another is
picked. When the chat runs on MLX, cleanup uses the minimum tier's GGUF chat model, since it
needs llama-server's JSON-schema output.

An admin picks a model for each role on the **Models & memory** page. Any model the catalog
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

Sizes include the vision models' projector files. Every default has a permissive licence
(Apache-2.0); the catalog refuses to load otherwise. Two models with restricted licences can
be chosen but are never a default: Chandra OCR 2 for handwriting and Qwen2.5-Coder-3B. The
licence is shown, and has to be accepted, before any download. The catalog also has MLX
builds of the three Qwen3.5 chat models (see [MLX](#mlx)).

## Downloads

Models download from Hugging Face at their pinned revision, with resumable `.part` files,
each file checked against its SHA-256, and 1 GiB of disk always left free
(`downloads/manager.py`). The first run downloads only the chat model and the llama.cpp
build; the OCR and coding models download the first time they're needed, after asking. When a
new Lectern pins a newer revision, the installed model shows **Update**: the new files
download beside the old ones, which stay in use until the model is free. Models live in
`<data>/models/`, a folder each.

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

### MLX

On a Mac with Apple Silicon and mlx-lm installed (the Mac installer adds it), an admin can
turn MLX on under Models & memory. The chat model then runs from its MLX build once that's
downloaded; until then the GGUF build keeps running. MLX covers the `main` role only: OCR,
handwriting, cleanup and code stay on llama-server.

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
