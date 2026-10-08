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
(`systemctl --user`), listening on 127.0.0.1 only; nginx on the same machine is the way in
from your network. There's no login yet, so keep it to your local network: anyone who has
the access link can use it.

**Needs:** Linux on x86_64 with systemd, the NVIDIA driver for an NVIDIA GPU (no CUDA
toolkit: Lectern downloads a llama.cpp build that brings its own), Node.js to build from the
repository (`sudo dnf install nodejs`), nginx, and internet for the first run. Python isn't
needed: the installer brings its own.

1. **Clone Lectern and run one script, as yourself** (no sudo):
   ```sh
   git clone https://github.com/matasarei/lectern.git ~/.lectern/src
   cd ~/.lectern/src && ./install.sh
   ```
   It builds the app from that copy, installs it and starts it on `http://127.0.0.1:8765`.
   Without Node on the server, make a package on any machine that has it,
   `./packaging/make_package.sh` → `dist/lectern-<version>-linux.tar.gz`, copy that over,
   unpack it anywhere, and run its `./install.sh`.

   Everything goes in `~/.lectern` (`LECTERN_DIR` moves it):

   | | |
   |---|---|
   | `src` | this repository, where you update from |
   | `app` | the installed app, with its own uv and Python |
   | `data` | the database (`lectern.sqlite`), models, projects, the access token |
   | `lectern.env` | settings: the port, the public address |
   | `bin/lectern-url` | prints the access link |

   The service is `~/.config/systemd/user/lectern.service`. So that it starts at boot without
   you logging in, the installer turns on lingering, or prints the line if that needs admin
   rights (`sudo loginctl enable-linger $USER`). If the GPU has a render node and you aren't
   in the `render` group, it prints `sudo usermod -aG render,video $USER` too.
2. **Set the address you'll use** (the server's name or IP on your network), then restart:
   ```sh
   nano ~/.lectern/lectern.env      # LECTERN_PUBLIC_URL=http://lectern.lan
   systemctl --user restart lectern
   ```
3. **Add the nginx site:** copy `packaging/linux/nginx-lectern.conf` to
   `/etc/nginx/conf.d/lectern.conf`, set its `server_name` to the same address, then
   `sudo nginx -t && sudo systemctl reload nginx`. On Fedora/RHEL (SELinux), let nginx reach
   Lectern's port, and only that one: `sudo semanage port -a -t http_port_t -p tcp 8765`.
4. **Open it:** this prints the link with the access token. Open it on any computer on your
   network; the first run checks the hardware and downloads the chat model.
   ```sh
   ~/.lectern/bin/lectern-url
   ```

**Update:** `cd ~/.lectern/src && git pull && ./install.sh` (or run a newer package's
`./install.sh`). Your data and settings are kept, and if the install fails, the running
version stays as it was. If the new version doesn't start, it says so, keeps the previous one
and prints how to go back to it.
**Uninstall:** `./uninstall.sh` keeps `~/.lectern/data` and `~/.lectern/lectern.env`; add
`--purge` to remove them too. `~/.lectern/src` always stays.
**Logs:** `journalctl --user -u lectern`. They show the access link at start.
**It runs as you,** so it can read what you can. The scripts the chat writes are the part
that's isolated: they run in Podman or Docker, with only their project folder mounted.
Without either, that feature switches itself off; on Fedora, rootless Podman works for your
user as it is (`sudo dnf install podman`).

### Or in Docker

The same, in a container: it needs the NVIDIA Container Toolkit for the GPU, and runs on the
host's network so that Lectern still listens on the host's 127.0.0.1 and nginx is the way in.
```sh
./packaging/build_wheel.sh
docker build -f packaging/docker/Dockerfile -t lectern .
LECTERN_PUBLIC_URL=http://lectern.lan docker compose -f packaging/docker/compose.yaml up -d
docker compose -f packaging/docker/compose.yaml exec lectern lectern url
```
Models and projects live in the `lectern-data` volume. Scripts' sandbox is off in Docker unless
you mount a Podman or Docker socket into the container.

## Licence

Apache-2.0, see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Models are downloaded separately and keep their own licences; the app shows each model's licence before downloading it.
