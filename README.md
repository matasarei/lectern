# Lectern

A local, modular AI workbench. Projects are folders of chat and OCR sessions and the files they produce. Every model runs on your own machine through llama.cpp (CUDA, Vulkan, Metal, CPU), or MLX on Apple Silicon.

- **Minimum hardware:** 4 GB VRAM + 16 GB RAM
- **Recommended:** 8 GB VRAM + 16 GB RAM
- **Platforms:** Windows, Linux, macOS

Status: pre-MVP. The design is in [`docs/blueprint.html`](docs/blueprint.html). Open it in a browser.

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
  computer) and opens Lectern as the admin.
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

## Licence

Apache-2.0, see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Models are downloaded separately and keep their own licences; the app shows each model's licence before downloading it.
