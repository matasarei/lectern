<p align="center">
  <img src="docs/lectern-icon.png" width="128" height="128" alt="Lectern's icon: an open book with a red initial L">
</p>

<h1 align="center">Lectern</h1>

<p align="center">
  <a href="https://github.com/matasarei/lectern/actions/workflows/ci.yml"><img src="https://github.com/matasarei/lectern/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/licence-Apache--2.0-blue" alt="Licence: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/runs%20on-llama.cpp%20%7C%20MLX-2C6A58" alt="Runs on: llama.cpp, MLX">
  <img src="https://img.shields.io/badge/platforms-Linux%20%7C%20macOS-555" alt="Platforms: Linux, macOS">
  <img src="https://img.shields.io/badge/status-MVP-2C6A58" alt="Status: MVP">
</p>

<p align="center"><b>An AI workbench that runs on your own computer.</b></p>

Lectern is a local, modular AI workbench. You work in **projects**: folders of chats, OCR
sessions and the files they produce. Every model runs on your own machine, through llama.cpp
(CUDA, Vulkan, Metal, CPU) or MLX on Apple Silicon, so what you type and import stays on it;
the internet is for downloading the models and llama.cpp, and for web search once an admin
switches it on.

- **A chat that works on your files.** It finds, searches and reads a project's files, writes
  new ones, and asks before deleting anything.
- **Exact answers by running code.** For a calculation it writes a Python script and runs it
  in a sandbox, with no network, instead of guessing (when
  [Podman or Docker](#the-script-sandbox-podman-or-docker) is installed).
- **Web search, when you allow it.** Switched on, the chat can search the web and Wikipedia
  and save a page into the project, and asks you before each one: see
  [Web search](#web-search).
- **Scanned pages to text.** An OCR session reads a PDF or a page image page by page, printed
  or handwritten, asks you about the words it isn't sure of, and builds a Document you can
  export as Markdown, DOCX or ODT.
- **Models chosen for your hardware,** one for each job, each downloaded at a fixed version and
  checked against its checksum.
- **Accounts for a household.** Each person signs in to a workspace of their own, at home or,
  through a tunnel, from anywhere.

- **Hardware:** 4 GB VRAM + 16 GB RAM at least; 8 GB VRAM + 16 GB RAM recommended.
- **Installers:** Linux (x86_64, systemd), macOS (Apple Silicon, 13 or newer) and Windows (x64,
  10 or 11).
- **Status:** MVP. How it works: [`docs/`](docs/README.md). What was checked on which machine,
  and what's still to run by hand on Windows and Linux:
  [`docs/acceptance/m4.md`](docs/acceptance/m4.md).

## Quick start

You need Node.js, and on a Mac the Command Line Tools (`xcode-select --install`). Then, as
yourself (no sudo):

```sh
git clone https://github.com/matasarei/lectern.git ~/.lectern/src
cd ~/.lectern/src && make install      # or ./install.sh
```

- **On a Mac** an open-book icon appears in the menu bar: choose **Open Lectern**.
- **On Linux** it runs as your own service; `~/.lectern/bin/lectern-url` prints the link to
  open on that machine.

The first run checks the hardware, recommends a chat model and downloads it. Everything lives
in `~/.lectern`. Update with `cd ~/.lectern/src && git pull && make install`; remove it with
`make uninstall` (keeps your models, projects and settings) or `make purge` (asks, then
removes those too), in the same folder. With [just](https://just.systems) installed,
`just install`, `just uninstall` and `just purge` do the same, on Windows too.
The details for each system are below: [Linux](#install-on-a-linux-server),
[Mac](#install-on-a-mac), [Windows](#install-on-windows).

## Using it

1. **Make a project** on the projects page. It's a folder: everything in it stays there.
2. **Chat.** **New chat**, then ask, or attach files to the message. **Import files…** in the
   project's Overview adds files for every chat, and the search box looks through their text.
3. **Read scanned pages.** Import a PDF or an image, open it, and choose **Open as OCR
   session**. Lectern reads it page by page and asks about what it isn't sure of; answer in
   the OCR tab, then export the Document.
4. **Choose models** in **Models & memory**: which model does each job, what's downloaded, and
   how much of the GPU's memory they're using. See [Models](#models).
5. **Let others in.** Turn accounts on with `~/.lectern/bin/lectern user init <your name>`,
   add people on the Users page, and give them a way in: see
   [Accounts](#accounts-a-login-for-each-person) and
   [Reaching it from other computers](#reaching-it-from-other-computers).

### The script sandbox: Podman or Docker

Answers that need exact numbers (sums, dates, counts) come from a script Lectern runs in a
container, with no network and only the files it was given. That needs Podman or Docker on
this computer; without one, Lectern still works, and marks such answers "not computed".
Podman is tried first. Lectern looks for either on its `PATH`, and checks again on its own
once one is running (first run has a **Check again** button).

- **Mac:** [Podman](https://podman.io/docs/installation#macos)
  (`brew install podman`, then `podman machine init` and `podman machine start`), or
  [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/). Both run a
  small Linux machine; start it before you ask for a calculation.
- **Linux:** Podman from your distribution (`sudo dnf install podman`,
  `sudo apt install podman`); it needs no service of its own. Or Docker Engine, with your
  user in the `docker` group, which amounts to root on that machine (Podman needs no such
  group).
- **Windows:** [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)
  or [Podman Desktop](https://podman-desktop.io/), both on WSL 2 (`wsl --install`, then
  restart).

The first calculation builds the sandbox's image once, and asks first: it downloads about
250 MB and takes about 430 MB on disk.

### Web search

Off until an admin switches it on in **Models & memory**, under **Web search**; it then
applies to every chat on this Lectern, a member's too. The chat can then:

- **search the web**, for anything specific or current (an error message, a software version,
  news). The query goes to the first of these that can answer:
  1. the **Brave Search API**, if an admin saved a key for it under **Web search** (get one
     at [brave.com/search/api](https://brave.com/search/api/)). The key stays on this
     computer, in a file only Lectern's user can read, and the page never shows it again;
  2. **DuckDuckGo** (`html.duckduckgo.com`), then **Yahoo** (`search.yahoo.com`). Neither has
     an API for this: the [ddgs](https://pypi.org/project/ddgs/) library reads their results
     pages, and they limit how often one computer may search. So each rests 30 seconds after
     every search, for all chats together, and a search goes to whichever isn't resting.
     When neither can answer, the chat says it's waiting and carries on by itself; after four
     tries it asks whether to **Try again** or **Stop**. With a key, these two are only the
     fallback;
- **look a subject up on Wikipedia**, in the question's language, through Wikipedia's own API
  (`<language>.wikipedia.org`);
- **open a page** by its address and save it in the project, as Markdown (a PDF stays a PDF),
  with the address in its **Where it came from**. Only public addresses are opened: this
  computer, its local network and cloud metadata addresses are refused, also after a
  redirect, and a page stops at 2 MB or 20 seconds.

Each one shows a card in the chat first, with the words or the address it would send and
where, and nothing is sent until you choose **Allow**. A queued follow-up (`task.after`) can't
use them, since nobody would be there to answer. What a page says is given to the chat as
data, never as instructions.

**Working on Lectern itself:** `make` installs the Python and web dependencies and builds the
web UI; `uv run lectern serve` then runs it from the checkout on `http://127.0.0.1:8765`. How
the code is laid out and checked is in [`CLAUDE.md`](CLAUDE.md).

## Install on a Linux server

The main way to run Lectern is straight on a machine's own OS, so the models get the GPU
directly: a general-purpose or older Linux computer with 4 GB of VRAM and 16 GB of RAM is
the target. Lectern installs for you, in your home folder, and runs as your own service
(`systemctl --user`). It listens on `127.0.0.1:8765` and knows nothing about what sits in
front of it: reaching it from other computers is up to you (see
[Reaching it from other computers](#reaching-it-from-other-computers)).

**Needs:** Linux on x86_64 with systemd, the NVIDIA driver for an NVIDIA GPU (no CUDA
toolkit: Lectern downloads a llama.cpp build that brings its own), Node.js to build from the
repository (`sudo dnf install nodejs`), and internet for the first run. Python isn't needed:
the installer brings its own.

1. **Clone Lectern and run one script, as yourself** (no sudo):
   ```sh
   git clone https://github.com/matasarei/lectern.git ~/.lectern/src
   cd ~/.lectern/src && ./install.sh
   ```
   It builds the app from that copy, installs it and starts it on `http://127.0.0.1:8765`.
   Without Node on the server, make a package on any machine that has it,
   `./packaging/make_package.sh` → `dist/lectern-<version>-linux.tar.gz`, copy that over,
   unpack it anywhere, and run its `./install.sh`.

   Everything goes in `~/.lectern`, wherever the copy you ran it from is (`LECTERN_DIR`
   moves it):

   | | |
   |---|---|
   | `src` | this repository, where you update from |
   | `app` | the installed app, with its own uv and Python |
   | `data` | the database (`lectern.sqlite`), models, projects, the access token |
   | `lectern.env` | settings: the port, the public address |
   | `bin/lectern-url` | prints the access link |
   | `bin/lectern` | Lectern's own command with those settings loaded, for `lectern user …` |

   The service is `~/.config/systemd/user/lectern.service`. So that it starts at boot without
   you logging in, the installer turns on lingering, or prints the line if that needs admin
   rights (`sudo loginctl enable-linger $USER`). If the GPU has a render node and you aren't
   in the `render` group, it prints `sudo usermod -aG render,video $USER` too (Vulkan needs
   it; CUDA doesn't).
2. **Open it:** this prints the link with the access token. On the machine itself it works
   as it is; from other computers, set up a way in first (below).
   ```sh
   ~/.lectern/bin/lectern-url
   ```
   The first run checks the hardware and downloads the chat model: the one recommended for
   the computer, or another you pick from its list.

**Update:** `cd ~/.lectern/src && git pull && ./install.sh` (or run a newer package's
`./install.sh`). Your data and settings are kept, and if the install fails, the running
version stays as it was. If the new version doesn't start, it says so, keeps the previous one
and prints how to go back to it.
**Uninstall:** `./uninstall.sh` keeps `~/.lectern/data` and `~/.lectern/lectern.env`; add
`--purge` to remove them too. `~/.lectern/src` always stays.
**Logs:** `journalctl --user -u lectern`. They show the access link at start, so keep the
journal to people you'd give the link to.
**It runs as you,** so it can read what you can. The scripts the chat writes are the part
that's isolated: they run in Podman or Docker, with only their project folder mounted.
Without either, that feature switches itself off; on Fedora, rootless Podman works for your
user as it is (`sudo dnf install podman`).

### Accounts: a login for each person

As installed, Lectern has one user and no login: whoever has the access link can do
everything. Before anyone else uses it, and before it's reachable from the internet, turn
accounts on. On the server, as the user Lectern runs as:

```sh
~/.lectern/bin/lectern user init <name>
```

`<name>` is what you'll sign in as. It asks for a password, twice, and makes you the admin,
keeping your projects. Passwords are always typed when asked, never given on the command
line. Use that `lectern`, not the one inside `app`: it loads Lectern's settings first, and
without them the command looks for Lectern's data somewhere else. If `~/.lectern/bin/lectern`
isn't there, your install is older than it: update first (`git pull && ./install.sh`). In
Docker it's `docker compose -f packaging/docker/compose.yaml exec lectern lectern user init
<name>`. Nothing needs restarting. From then on:

- **People sign in** with a name and a password, at the address in `LECTERN_PUBLIC_URL`.
  There's no sign-up: you add each person, on the Users page (linked beside your name on the
  projects page) or with `lectern user add <name>`, run the same way. Each gets a workspace
  of their own, in `~/.lectern/data/users/<name>/projects`; yours stays in
  `~/.lectern/data/users/local`.
- **A password** is 15 characters or more. A sentence is fine, and nothing else is asked of
  it. Five wrong ones in a row make that account wait a minute, then longer each time, up to
  15 minutes. A browser stays signed in for 30 days, or 7 without use.
- **The access link works on the server itself only** (`127.0.0.1` or `localhost`, from that
  computer) and opens Lectern as the admin you made with `lectern user init`. Not while
  that account is disabled: `lectern user enable <name>` on the server lets it in again.
- **A forgotten password** is set again on the server: `lectern user password <name>`, run
  the same way, asks for a new one and signs that person out everywhere. That works for
  your own too.
- **Only an admin** downloads, removes or chooses models, changes how they're loaded, runs
  the first-run setup, and manages accounts. Everyone else sees which models there are, and
  uses them.
- **Nobody sees another person's projects through Lectern, the admin included.** But
  projects are ordinary folders on the server, and whoever runs the server can read them
  there. Tell the people you add.

`lectern user list` shows who has an account; `lectern user disable <name>` stops someone
signing in and signs them out, keeping their projects, and `enable` undoes it. On a Mac it's
all the same, with the same paths.

### Reaching it from other computers

Lectern listens on `127.0.0.1` unless you say otherwise. To use it from elsewhere, put
something in front of it that forwards to `http://127.0.0.1:8765`, or let it listen on your
network itself (the last option below). Either way, tell Lectern the address people will use,
because it refuses requests for any other host or origin:

```sh
nano ~/.lectern/lectern.env      # LECTERN_PUBLIC_URL=https://lectern.example.com
systemctl --user restart lectern
```

`LECTERN_PUBLIC_URL` takes one or more `http(s)://host[:port]`, comma-separated, exactly as
they appear in the browser's address bar. Whatever you put in front has to pass the `Host`
header through unchanged and allow WebSockets and streamed responses (the chat and progress
updates). How you set that up is yours; three ways that work:

- **A Cloudflare Tunnel, with Cloudflare Access** (from anywhere, nothing opened on the
  server). In the tunnel, point a public hostname at `http://127.0.0.1:8765` and leave
  `httpHostHeader` unset. **Turn accounts on first** (above), and write the hostname with
  `https://` in `LECTERN_PUBLIC_URL`: that is what makes Lectern mark its login cookie
  `Secure` and tie it to that host. Then put a Cloudflare Access application in front of the
  hostname all the same (Zero Trust → Access → Applications, a policy allowing only the
  emails of the people you added, with a one-time code): nobody reaches Lectern's sign-in
  page without passing Cloudflare's first. The price is signing in twice the first time. On
  the free plan Cloudflare refuses uploads over 100 MB, and cuts a request after about 100
  seconds without a response.
- **nginx on the same machine** (your local network). `packaging/linux/nginx-lectern.conf`
  is an example: copy it to `/etc/nginx/conf.d/lectern.conf`, set its `server_name`, then
  `sudo nginx -t && sudo systemctl reload nginx`. On Fedora/RHEL (SELinux), let nginx reach
  Lectern's port, and only that one: `sudo semanage port -a -t http_port_t -p tcp 8765`.
  As written it's plain http: with accounts on, people sign in, but a password typed over it
  can be read by someone watching the network. Keep it to a network you trust, or give it a
  certificate and put the `https://` address in `LECTERN_PUBLIC_URL`.
- **Directly on your network, no proxy.** Let Lectern listen on every address, and use the
  machine's own address as the public one:
  ```sh
  # ~/.lectern/lectern.env
  LECTERN_HOST=0.0.0.0
  LECTERN_PUBLIC_URL=http://192.168.1.2:8765      # the IP or name people type, with the port
  ```
  then `systemctl --user restart lectern`. `LECTERN_HOST` takes `127.0.0.1` (the default) or
  `0.0.0.0`, nothing else, and `0.0.0.0` needs `LECTERN_PUBLIC_URL`. If the firewall blocks
  the port (Fedora Workstation allows 1025–65535; Server doesn't):
  `sudo firewall-cmd --add-port=8765/tcp --permanent && sudo firewall-cmd --reload`.
  **The risks:** until you turn accounts on (above), anyone on the network who has the link
  can use Lectern. With them on, the link stops working from other computers and people sign
  in. Either way it's plain http, so on Wi-Fi or any network you don't control, a password
  or the token can be read off the network by someone watching it. Use it only on a network
  you trust, never forward the port on your router, and use the tunnel with Access for
  anything wider. The model servers Lectern runs stay on `127.0.0.1` either way.

### Or in Docker

The same, in a container: it needs the NVIDIA Container Toolkit for the GPU, and runs on the
host's network so that Lectern still listens on the host's 127.0.0.1 only; the way in is the
same as above.
```sh
./packaging/build_wheel.sh
docker build -f packaging/docker/Dockerfile -t lectern .
LECTERN_PUBLIC_URL=http://lectern.lan docker compose -f packaging/docker/compose.yaml up -d
docker compose -f packaging/docker/compose.yaml exec lectern lectern url
```
Models and projects live in the `lectern-data` volume. Scripts' sandbox is off in Docker unless
you mount a Podman or Docker socket into the container.

## Install on a Mac

On a Mac, Lectern runs straight on macOS, never in Docker: a container there can't reach the
GPU. The same script installs it for you and builds **Lectern.app**, a small menu-bar app
that starts and stops it. Lectern runs for as long as the app is open.

**Needs:** an Apple Silicon Mac (Intel Macs aren't supported yet) with macOS 13 or newer, the
Command Line Tools (`xcode-select --install`), Node.js (`brew install node`, or the installer
from nodejs.org), and internet for the first run. Python isn't needed: the installer brings
its own.

1. **Clone Lectern and run one script, as yourself** (no sudo):
   ```sh
   git clone https://github.com/matasarei/lectern.git ~/.lectern/src
   cd ~/.lectern/src && ./install.sh
   ```
   It builds Lectern and the app from that copy, puts the app in `~/Applications` and opens
   it: an icon appears in the menu bar. Everything else goes in `~/.lectern`, laid out as
   [on Linux](#install-on-a-linux-server) (`src`, `app`, `data`, `lectern.env`,
   `bin/lectern-url`, `bin/lectern`). The app is built on your own Mac, so macOS opens it
   without the warning it shows for a downloaded one. Its icon, an open book with an L on
   its first page, is drawn there too.
2. **Use the icon's menu:**

   | | |
   |---|---|
   | Open Lectern | opens it in your browser, signed in (the link carries the access token) |
   | Stop / Start, Restart | Lectern itself; the app stays in the menu bar |
   | Show Log | `~/Library/Logs/Lectern/lectern.log`; the access link is never written to it |
   | Shut Down Lectern | stops Lectern and every model it has loaded, then closes the app |

   Opening Lectern.app again (Finder, Spotlight) opens it in the browser too. The first run
   checks the hardware, lets you pick the chat model and the engine it runs on (llama.cpp
   or MLX, below), and downloads them.

**Update:** `cd ~/.lectern/src && git pull && ./install.sh`. An open app is quit, replaced and
opened again; one that wasn't open is replaced and left closed. Your data and settings are
kept, and if anything fails to build, the running app and version stay as they were.
**Start at login:** System Settings → General → Login Items → add Lectern.
**Settings:** `~/.lectern/lectern.env`, then Restart in the menu.
**Uninstall:** `./uninstall.sh` removes the app, its log and `~/.lectern/app`; it keeps
`~/.lectern/data` and `~/.lectern/lectern.env` unless you add `--purge`. `~/.lectern/src`
always stays.
**The script sandbox** needs Docker Desktop or Podman; Lectern looks for them in
`/opt/homebrew/bin` and `/usr/local/bin` as well as the system's folders.
**If you also run Lectern from a checkout** (`uv run lectern serve`): both use port 8765, so
run one at a time or change `LECTERN_PORT` in `~/.lectern/lectern.env`. The installed one
keeps its own data in `~/.lectern/data`; the installer prints the line that lets it use the
models a checkout already downloaded.

### MLX, if you want it

Models run on llama.cpp (Metal) by default, on a Mac as everywhere. The installer also adds
MLX, Apple's own engine, and the first run offers the two side by side, llama.cpp selected.
Pick MLX there and only the chat model's MLX build is downloaded: llama.cpp and a small
model that cleans the pages up (2.7 GB) come the first time you scan a page. Asked for in
the chat, the scan says what it downloads first; started from a file's own button, it tells
you which model to download in Models & memory.

You can change your mind later in **Models & memory** → "Use MLX for MLX models". The chat
model then shows its MLX build: download that, and the chat runs on it. A chat model already
on disk goes on running until the build is there; with neither on disk, the build is the one
Models & memory offers.

What it gives and what it costs, measured with the 4B chat model on an M4 Max:

- **Faster replies:** about 124 tokens a second against 105.
- **More mistakes with tools:** MLX can't hold the model to the exact shape a tool expects,
  so the chat gets a tool call wrong more often and has to be told.
- **Scanned pages are cleaned by a model on llama.cpp,** for the same reason: the 4B chat
  model, unless you choose another on the `clean` row of "Models by role". While an OCR
  session is cleaned, it is in memory beside the chat's model.
- **Long conversations count their size by estimate,** not exactly, so old messages may be
  summarised a little early or late.

OCR models always run on llama.cpp. Lectern starts MLX's server with a key of its own, as it
does llama.cpp's: a web page can't use the model, and neither can another user of the Mac.

## Install on Windows

On Windows, Lectern runs straight on the PC, with a small tray app (by the clock) that starts
and stops it. Lectern runs for as long as the tray app does.

**Needs:** a 64-bit PC with Windows 10 or 11, Git and Node.js
(`winget install Git.Git OpenJS.NodeJS.LTS`, then open a new window), and internet for the
first run. Python isn't needed: the installer brings its own. The tray app is compiled on
your PC by the C# compiler that comes with Windows (.NET Framework 4). Optional:
[just](https://just.systems) (`winget install Casey.Just`), for the shorter commands below.

1. **Clone Lectern and run one script, as yourself** (not as administrator), in PowerShell:
   ```powershell
   git clone https://github.com/matasarei/lectern.git $HOME\.lectern\src
   cd $HOME\.lectern\src; powershell -ExecutionPolicy Bypass -File install.ps1
   ```
   With just, the second line is `cd $HOME\.lectern\src; just install`.
   Everything goes in `%USERPROFILE%\.lectern`, laid out as
   [on Linux](#install-on-a-linux-server) (`src`, `app`, `data`, `lectern.env`,
   `bin\lectern-url.cmd`, `bin\lectern.cmd`). The installer adds Lectern to the Start menu and
   to the Startup folder, and starts it.
2. **Use the tray icon's menu** (right-click it; double-click opens Lectern). The icon is a book,
   open while Lectern runs and closed while it's stopped; it and the menu follow the taskbar's
   light or dark mode:

   | | |
   |---|---|
   | Open Lectern | opens it in your browser, signed in (the link carries the access token) |
   | Stop / Start, Restart | Lectern itself; the tray app stays |
   | Show Log | `%LOCALAPPDATA%\Lectern\Logs\lectern.log`; the access link is never written to it |
   | Quit Lectern | stops Lectern and every model it has loaded, then closes the tray app |

   Opening Lectern again from the Start menu opens it in the browser. The first run checks
   the hardware, lets you pick the chat model, and downloads it with the llama.cpp build for
   your GPU (CUDA, Vulkan or the CPU).

**Update:** `cd $HOME\.lectern\src; git pull; powershell -ExecutionPolicy Bypass -File install.ps1`
(or `just install` after the pull).
A running Lectern is quit, replaced and started again; one that wasn't running is replaced and
left stopped. Your data and settings are kept, and if anything fails to build, the running
Lectern and its version stay as they were.
**Start at sign-in:** the installer puts it in the Startup folder; delete the shortcut there
(`shell:startup` in the Run box) to stop that.
**Settings:** `%USERPROFILE%\.lectern\lectern.env`, then Restart in the menu.
**Uninstall:** `powershell -ExecutionPolicy Bypass -File uninstall.ps1` removes the tray app,
its shortcuts, its log and `.lectern\app`; it keeps `.lectern\data` and `.lectern\lectern.env`
unless you add `-Purge`, which asks first. `.lectern\src` always stays. With just:
`just uninstall`, or `just purge` for `-Purge`.
**Build for development** (in your copy of the repository): `just` runs `npm ci` when the lock
file has changed, then `uv sync` and the web build, as `make` does on Linux and the Mac.
**The script sandbox** needs Docker Desktop or Podman Desktop (both on WSL 2): see
[The script sandbox](#the-script-sandbox-podman-or-docker).
**Even if the tray app is ended** (Task Manager), nothing is left behind: Lectern and every
model runner it started end with it.

## Models

Lectern uses a model for each job: one for the chat, two for reading scanned pages (print and
handwriting), one for writing scripts. It picks them from the computer it finds itself on. The
first run lets you pick the chat model; everything else about them is on the **Models &
memory** page.

- **Choosing.** Under "Models by role" each role has a list of the models it can use. The one
  marked *recommended* is the default for this computer's hardware. A model marked as more
  than this computer is meant for can still be chosen: it may be slow, run with a small
  context, or not load. A change takes effect with the next reply, without a restart. After
  a change of hardware (a new video card) the *recommended* mark moves, and a role goes on
  using the model it already has until you switch. Cleaning up scanned pages has a row of
  its own (`clean`): the chat's own model does it unless you pick another, or the chat runs
  on MLX.
- **What's installed.** "Installed models" lists what's on disk: each model's version (the
  Hugging Face revision it was downloaded at), its size and the roles using it. **Remove**
  deletes a model after asking; one that's in use isn't removed. A role whose model was
  removed shows Download.
- **Updates.** Lectern downloads every model at one exact revision, with a checksum for each
  file, and a new Lectern can move a model to a newer revision. The installed copy then shows
  **Update**: the new version is downloaded beside the old one, which stays in use, and takes
  its place when the model is free.

The models are in `~/.lectern/data/models`, one folder each.

## Security

How Lectern is protected, what it doesn't protect against, and how to report a vulnerability
privately: [`SECURITY.md`](SECURITY.md).

## Licence

Apache-2.0, see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Models are downloaded separately and keep their own licences; the app shows each model's licence before downloading it.
