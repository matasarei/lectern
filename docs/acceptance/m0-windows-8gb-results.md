# M0 acceptance run: Windows PC, 8 GB NVIDIA (recommended tier)

Run on 2026-09-29 against [`m0-windows-8gb.md`](m0-windows-8gb.md), on branch
`fix/cuda-build-for-older-gpus` (since merged as PR #3), llama.cpp build `b11236`. The summary
lands in [`m0.md`](m0.md); this page keeps the raw output and the two findings.

**Verdict: 2 of 3 targets met.** Chat speed and the swap test pass. First token after a model
load misses its 5 s target on every run. Separately, `lectern serve` does not exit on Ctrl+C.

```text
Machine: Intel Core Ultra 7 155H (16 cores), NVIDIA GeForce RTX 4060 Laptop GPU 8188 MiB,
16 GB RAM (15.4 GB usable), Windows 11 Pro 10.0.26200, NVIDIA driver 610.62 (CUDA 13.3)
```

| Check | Result | Target | |
|---|---|---|---|
| First token after a model load | 8.97 s / 6.61 s / 5.92 s over three runs | ≤ 5 s | **fail** |
| Chat speed, median of 3 | 33.4 tokens/s | ≥ 30 | pass |
| Swap test, 12 pages, 7000 MiB | 1 swap | 1 | pass |
| Tier detection | `recommended` | — | pass |
| Runner build choice | `windows-cuda13` (cc 8.9, CUDA 13.3 driver) | — | pass |
| GPU memory in use while loaded | 5797 MiB of 8188 MiB | fits 8 GB | pass |
| First chat on a fresh install (includes the 712 MB llama.cpp download) | 81.6 s | — | expected once |
| `llama-server` processes left after Ctrl+C | 0 | 0 | pass |
| `lectern serve` itself exits on Ctrl+C | no, hangs indefinitely | exits | **fail** |

## 1. Setup

Prerequisites installed with `winget`: uv 0.12.20, Node 24.19.0 (npm 11.17.0), Git 2.53.0,
gh 2.97.0. Then `uv sync`, `npm --prefix web ci` and `npm --prefix web run build` — all clean,
the web build finished in 1.47 s.

No Windows Defender or SmartScreen prompt appeared for `llama-server.exe`, and no firewall
dialog for it or for Python.

Disk used under `%USERPROFILE%\lectern-test\`: 5.3 GB models, 712 MB runners.

## 2. `doctor --runners`

```json
{
  "hardware": {
    "os": "windows",
    "arch": "AMD64",
    "cpu": "Intel64 Family 6 Model 170 Stepping 4, GenuineIntel",
    "cpu_cores": 16,
    "ram_total": 16556224512,
    "ram_available": 6668308480,
    "gpus": [
      {
        "vendor": "nvidia",
        "name": "NVIDIA GeForce RTX 4060 Laptop GPU",
        "memory_total": 8585740288,
        "memory_free": 8342470656,
        "unified": false,
        "cuda_driver": "13.3",
        "compute_capability": "8.9"
      }
    ],
    "gpu_budget": 7941495194
  },
  "tier": "recommended",
  "tier_summary": "9B chat model; OCR swaps with the chat model.",
  "disk_free": 855301681152,
  "sandbox": {
    "runtime": "docker",
    "version": null,
    "engine_running": false,
    "found": ["docker"]
  },
  "runners": {
    "llama_cpp_build": "b11236",
    "variant": "windows-cuda13",
    "installed": false,
    "server": null
  }
}
```

Correct on every axis: compute capability 8.9 with a CUDA 13 driver takes the `windows-cuda13`
branch, as the instructions predict.

The `windows-cuda13` build reports `ARCHS = 750,800,860,890,900,1200,1210` — no 610, so a Pascal
card really would have no code here. That is the case this branch exists to fix; it could not be
exercised on this machine, which is Ada.

## 3. First run

The first-run wizard reported the machine correctly: Recommended tier, 15 GB memory, 7.4 GB
video memory for models. Continue, then Download (5.7 GB at about 11.5 MB/s), then Finish setup
all worked.

Two notes on the wizard, neither blocking:

- It shows an **"Exact calculations"** panel the instructions do not mention: *"Docker is
  installed but not running. Start it to let Lectern run calculation scripts."*
- After Finish setup the app lands on **Models & memory**. The OCR, handwriting and code models
  show as "Not downloaded", as intended.

## 4. Measurements

### bench

First run, which also downloads and installs llama.cpp:

```text
tier                         recommended
first token after a load     81.637 s               ≤ 5.0 s
chat speed (median of 3)     33.5 tokens/s          ≥ 30.0
model                        qwen3.5-9b
BELOW TARGET
```

Second run, the one that counts:

```text
tier                         recommended
first token after a load     8.968 s                ≤ 5.0 s
chat speed (median of 3)     33.4 tokens/s          ≥ 30.0
model                        qwen3.5-9b
BELOW TARGET
```

Two more runs followed, to see whether the first-token figure was a warm-up artifact:

| run | first token | chat speed |
|---|---|---|
| 1 (includes the llama.cpp download) | 81.637 s | 33.5 tokens/s |
| 2 | 8.968 s | 33.4 tokens/s |
| 3 | 6.605 s | 33.1 tokens/s |
| 4 | 5.919 s | 32.4 tokens/s |

First token falls as the OS file cache warms to the 5.7 GB GGUF, but never reaches 5 s. Chat
speed is stable and clears its target with room to spare. Every run printed `BELOW TARGET` and
exited 1.

### swap-test

```text
pages: 24/24
swaps: 1
```

Exactly one swap, as required. (`--pages 12` reports 24 items: an OCR and a chat phase per page.)

### nvidia-smi while loaded

```text
| N/A   57C    P8              3W /   42W |    5797MiB /   8188MiB |      0%      Default |
|    0   N/A  N/A           17328      C   ...s-cuda13\bin\llama-server.exe      N/A      |
```

5797 MiB of 8188 MiB held by `llama-server.exe`. The Models & memory gauge agreed: "5.5 GB of
7.4 GB in use — main: qwen3.5-9b, 5.5 GB, 16384-token context". The per-process memory column
reads `N/A`, which is normal under WDDM.

### After Ctrl+C

```text
PS> Get-Process llama-server -ErrorAction SilentlyContinue
PS>
```

Empty — no runner left. But see finding 2: the `lectern serve` process itself is still there.

## Finding 1 — the 5 s first-token target is not reachable for a 9B model on this card

The time is spent inside llama.cpp, not in Lectern. From
`%USERPROFILE%\lectern-test\run\qwen3.5-9b\server.log` on the 5.919 s run:

```text
0.00.848.071 I srv    load_model: loading model '...\Qwen3.5-9B-Q4_K_M.gguf'
0.01.785.224 I load_tensors: loading model tensors, this can take a while... (load_mode = mmap)
0.02.332.170 I load_tensors: offloaded 33/33 layers to GPU
0.02.332.178 I load_tensors:        CUDA0 model buffer size =  4861.28 MiB
0.05.587.595 I srv  llama_server: model loaded
```

**4.74 s** passes between "loading model" and "model loaded" — before Lectern sends a single
token. Of that, **3.25 s is after all 33 layers are already on the GPU**: `llama_context`, the
512 MiB KV cache, a 201 MiB recurrent-state buffer and a 104 MiB compute buffer, from the same
log:

```text
0.04.618.078 I llama_kv_cache:      CUDA0 KV buffer size =   512.00 MiB
0.04.640.959 I llama_memory_recurrent:      CUDA0 RS buffer size =   201.00 MiB
0.04.658.537 I sched_reserve:      CUDA0 compute buffer size =   104.02 MiB
```

Lectern's own share is roughly 1.2 s: the preceding unload, the process spawn, and the 0.25 s
`/health` poll interval in `src/lectern/runners/llamacpp.py`. Tightening that would not close a
4.74 s gap against a 5 s budget.

Nothing is misconfigured. All 33 of 33 layers are offloaded, and prompt eval runs at 165 to 340
tokens/s:

```text
slot print_timing: prompt eval time = 151.28 ms / 25 tokens (6.05 ms per token, 165.25 t/s)
slot print_timing: prompt eval time =  73.22 ms / 25 tokens (2.93 ms per token, 341.44 t/s)
```

So the 5 s target for the recommended tier in `TARGETS` in `src/lectern/cli.py` needs a
decision, not a code fix:

- raise it for the recommended tier on Windows (7 to 9 s would pass here), **or**
- stop counting CUDA context and KV-cache setup as "first token", measuring from
  `model loaded` instead, **or**
- accept it as a known miss for a 9B model on an 8 GB card and document it.

The MVP plan reads "first token after a model load" as a stand-in for a swap. On that reading
the load cost is real and belongs in the number, which argues for raising the target rather than
changing what is measured.

## Finding 2 — `lectern serve` does not exit on Ctrl+C

Reproduced by delivering a genuine `CTRL_C_EVENT` to the serve console.

What happens:

1. `llama-server.exe` exits. It shares the console, so it receives the signal directly — its
   exit does not by itself prove Lectern's cleanup ran.
2. Port 8765 stops accepting connections. `Invoke-WebRequest http://127.0.0.1:8765/health` gets
   connection refused.
3. **The `lectern serve` Python process never exits.** It was still resident 92 s after the
   signal, and then indefinitely, until force-killed.

The cause is uvicorn's graceful shutdown waiting on long-lived SSE streams. While hung, the
process held two established connections:

```text
Proto  Local            Remote     Owning process
TCP    127.0.0.1:8765   :54981     msedge (pid 21508)
TCP    127.0.0.1:8765   :57218     msedge (pid 21508)
```

Those are `/api/runtime/events` and `/api/models/events`, held by the browser tab **that
`lectern serve` itself opened at startup**. So the default path hits this every time: start the
app, press Ctrl+C, and the process hangs.

This matters for the acceptance check as written. Step 5 tells the tester to run
`Get-Process llama-server`, which comes back empty, so the machine looks clean while
`lectern serve` is still running and still holding port 8765. A second `lectern serve` will fail
to bind.

Suggested fixes, either alone would do:

- close the SSE generators on the FastAPI lifespan shutdown event, so uvicorn has nothing left
  to drain;
- pass `timeout_graceful_shutdown` to uvicorn so it gives up on stuck connections.

Step 5 of the instructions should also check for the app process, not only the runner:

```powershell
Get-Process llama-server -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*lectern*" }
```

## Smaller observations

- **Run directory names contain spaces and parentheses.** The swap test creates
  `%USERPROFILE%\lectern-test\run\qwen3.5-9b (as ocr)\`. It worked here, but it is fragile on
  Windows if that path ever reaches an unquoted command line or a tool that splits on spaces.
- **A cold `lectern chat` right after a fresh `serve` took 27.6 s to first token**, against
  about 6 s for the same load inside `bench`. `bench` unloads an already-warm server, so its
  "cold" is warmer than a genuine first chat. Worth a look: it also means the number a user
  first experiences is well above the benchmarked one.
- **`bench` exits 1 on BELOW TARGET.** Correct for CI, but worth knowing when scripting a run.
- Docker is installed on this machine but not running, so the sandbox reported
  `engine_running: false`. Nothing in this acceptance run needed it.

## Environment caveat, not a Lectern bug

`uv sync` failed on its first attempt:

```text
error: Missing expected target directory for Python minor version link at
%APPDATA%\uv\python\cpython-3.12.14-windows-x86_64-none
```

The shell used for this run lives inside a packaged (MSIX) container that redirects `%APPDATA%`
to a per-package `LocalCache` path, which breaks the junction uv creates for the minor-version
link. Setting `UV_PYTHON_INSTALL_DIR` and `UV_CACHE_DIR` outside `%APPDATA%` resolved it.

A tester in an ordinary PowerShell window will not hit this. It is recorded only to explain why
this run's transcript differs from the instructions.
