# M0 acceptance on Windows, 8 GB card: what to run and what to send back

This is the last open item of M0, step 10 of the runtime plan: the **recommended tier**, an 8 GB
NVIDIA card with 16 GB RAM. It's also Lectern's **first run on Windows**, so rough edges are
expected. Finding them is part of the point. Results go into [`m0.md`](m0.md).

Targets for the recommended tier:

| Check | Target |
|---|---|
| Chat speed (median of 3) | ≥ 30 tokens/s |
| First token after a model load | ≤ 5 s |
| Swap test, 12 pages, 7000 MiB budget | exactly 1 swap |

## What it downloads and where it puts things

Everything goes into two folders, and deleting both undoes the whole test:

- `lectern\`: the repository clone, wherever you run `gh repo clone`;
- `%USERPROFILE%\lectern-test\`: models, the llama.cpp build, and app data.

Downloads, roughly:

| What | Size | When |
|---|---|---|
| Python 3.12 and packages (through uv) | ~250 MB | first `uv sync` |
| Qwen3.5-9B chat model | 5.7 GB | first run in the browser |
| llama.cpp for Windows + CUDA runtime | 0.6–0.7 GB | first chat |

The app only listens on 127.0.0.1. If Windows Defender Firewall asks about `llama-server.exe`
or Python, **Cancel/deny is fine**: nothing needs to be reachable from the network.

## 1. One-time setup (PowerShell)

```powershell
winget install --id astral-sh.uv
winget install OpenJS.NodeJS.LTS
winget install Git.Git
winget install GitHub.cli
```

Close and reopen PowerShell so the new tools are on `PATH`. Then:

```powershell
gh auth login
gh repo clone matasarei/lectern
cd lectern
git checkout fix/cuda-build-for-older-gpus
uv sync
npm --prefix web ci
npm --prefix web run build
```

Use that branch until it's merged: it holds the fix for older NVIDIA cards (Pascal, e.g.
GTX 1070/1080), which some 8 GB cards are.

## 2. Check the machine

In every PowerShell window you use for the test, set this first:

```powershell
$env:LECTERN_HOME = "$HOME\lectern-test"
```

Then:

```powershell
uv run lectern doctor --runners
```

**Expected:** `"tier": "recommended"`, your card with its `compute_capability`, and `runners`
with `"variant": "windows-cuda13"` for compute capability 7.5 or newer (GTX 16xx, RTX), or
`"windows-cuda12"` for older cards. If the tier or variant looks wrong, stop here and send the
output: the rest depends on it.

## 3. Start the app and finish first run

```powershell
uv run lectern serve
```

It opens the browser. Click **Continue**, then **Download** (5.7 GB), then **Finish setup**.
Leave this window running.

> The line `Lectern is running at http://127.0.0.1:8765/#token=…` contains your install's access
> key. Don't paste that line anywhere.

## 4. Measure (a second PowerShell window)

```powershell
$env:LECTERN_HOME = "$HOME\lectern-test"
cd lectern
uv run lectern bench
uv run lectern bench
uv run lectern dev swap-test --pages 12 --budget-mb 7000 --fake-model
nvidia-smi
```

- **Why two benches:** the first `bench` also downloads and installs llama.cpp, so its "first
  token after a load" includes that download and isn't a real measurement. The second is the
  one that counts.
- **The swap test** loads a second copy of the chat model as a stand-in OCR model. Two copies of
  the 9B model don't fit in 7000 MiB, so it must swap exactly once.
- **`nvidia-smi`** shows how much of the card llama-server holds. Run it while `lectern serve` is
  still running.

Don't run the swap test with a larger budget (like 40000). That tells the app the card has
40 GB, and it would load a second model onto 8 GB. It already passed on the Mac.

## 5. Stop

Press `Ctrl+C` in the `lectern serve` window, then check nothing is left running:

```powershell
Get-Process llama-server -ErrorAction SilentlyContinue
```

It should print nothing.

## What to send back

Copy this, fill it in, and paste it into the chat (or commit it into `m0.md` under "Windows PC,
8 GB NVIDIA"):

````text
Machine: <CPU>, <GPU and its VRAM>, <RAM>, Windows <version>, NVIDIA driver <version>

## doctor --runners
<paste the whole JSON>

## bench (second run)
<paste the table>

## swap-test --budget-mb 7000
<paste the two lines>

## nvidia-smi while loaded
<paste the process table, or just "llama-server.exe: N MiB">

## Get-Process llama-server after Ctrl+C
<empty, or what it printed>

## Anything that went wrong
<the command, the error, and the last ~30 lines of
 $HOME\lectern-test\run\<model>\server.log if llama-server was involved>
````

## If something breaks

Don't try to work around it; send the error. A failure here is a Windows bug to fix, and the
exact message is what makes it fixable. Useful extras when something fails:

- `uv run lectern doctor` (without `--runners`), if the hardware looks wrong;
- `$HOME\lectern-test\run\<model>\server.log`, if a model wouldn't load or chat failed;
- whether Windows Defender or SmartScreen blocked `llama-server.exe` (it's an unsigned
  download from the llama.cpp GitHub release, checked against a pinned sha256 before it runs).

## Cleaning up afterwards

```powershell
Remove-Item -Recurse -Force "$HOME\lectern-test"
```

Then delete the `lectern` clone folder if you don't need it. The tools from step 1 stay
installed; remove them with `winget uninstall` if you like.
