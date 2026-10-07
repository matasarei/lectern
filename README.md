# Lectern

A local, modular AI workbench. Projects are folders of chat and OCR sessions and the files they produce. Every model runs on your own machine through llama.cpp (CUDA, Vulkan, Metal, CPU), or MLX on Apple Silicon.

- **Minimum hardware:** 4 GB VRAM + 16 GB RAM
- **Recommended:** 8 GB VRAM + 16 GB RAM
- **Platforms:** Windows, Linux, macOS

Status: pre-MVP. The design is in [`docs/blueprint.html`](docs/blueprint.html). Open it in a browser.

## Install on a Linux server

The main way to run Lectern is straight on a machine's own OS, so the models get the GPU
directly: a general-purpose or older Linux computer with 4 GB of VRAM and 16 GB of RAM is
the target. Lectern runs as a service and listens on 127.0.0.1 only; nginx on the same
machine is the way in from your network. There's no login yet, so keep it to your local
network: anyone who has the access link can use it.

**Needs:** Linux on x86_64 with systemd, the NVIDIA driver for an NVIDIA GPU (no CUDA
toolkit: Lectern downloads a llama.cpp build that brings its own), nginx, and internet for
the first run. Python isn't needed: the installer brings its own.

1. **Build the wheel** (on a machine with this repository, Node and uv):
   ```sh
   ./packaging/build_wheel.sh        # → dist/lectern_ai-<version>-py3-none-any.whl
   ```
   Copy the wheel and the `packaging/` folder to the server.
2. **Install** (on the server):
   ```sh
   sudo ./packaging/linux/install.sh lectern_ai-<version>-py3-none-any.whl
   ```
   It creates the `lectern` user and service, puts the app in `/opt/lectern`, and starts it.
   Models and projects go in `/var/lib/lectern`; settings in `/etc/lectern/lectern.env`.
3. **Set the address you'll use** (the server's name or IP on your network), then restart:
   ```sh
   sudoedit /etc/lectern/lectern.env      # LECTERN_PUBLIC_URL=http://lectern.lan
   sudo systemctl restart lectern
   ```
4. **Add the nginx site:** copy `packaging/linux/nginx-lectern.conf` to
   `/etc/nginx/conf.d/lectern.conf`, set its `server_name` to the same address, then
   `sudo nginx -t && sudo systemctl reload nginx`. On Fedora/RHEL, let nginx reach a local
   port once: `sudo setsebool -P httpd_can_network_connect 1`.
5. **Open it:** this prints the link with the access token. Open it on any computer on your
   network; the first run checks the hardware and downloads the chat model.
   ```sh
   sudo -u lectern /opt/lectern/bin/lectern-url
   ```

**Upgrade:** run `install.sh` again with a newer wheel. Your settings and data are kept, and
if the install fails, the running version stays as it was.
**Uninstall:** `sudo ./packaging/linux/uninstall.sh` keeps `/var/lib/lectern` and
`/etc/lectern`; add `--purge` to remove them and the `lectern` user too.
**Logs:** `journalctl -u lectern`. They show the access link at start, so keep the journal to
people you'd give the link to.
**Scripts (the sandbox):** the chat runs the scripts it writes in Podman or Docker. Without
either, that feature switches itself off. To turn it on, install Podman and give the
`lectern` user rootless Podman (`sudo usermod --add-subuids 100000-165535
--add-subgids 100000-165535 lectern`).

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
