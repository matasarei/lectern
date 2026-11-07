"""A stand-in for sd-cli (a Python script with the same arguments) and what the image tests
share: the runtime, the downloads and the installer it needs."""

from __future__ import annotations

import sys
from pathlib import Path

from lectern.catalog import catalog
from lectern.core.db import make_engine, make_sessionmaker, migrate
from lectern.modules.image.generate import Images

# What the fake does is chosen by a word in the prompt:
#   FAIL     exits 1 after an error line;   NOPICTURE  exits 0 and writes nothing
#   NOTPNG   writes text as the picture;     WRONGSIZE  writes a 64x64 PNG
#   HANG     prints one step, then waits until it is killed (it writes its pid first)
# Anything else: 4 steps of progress, a VAE tile bar, then the picture, with a metadata chunk.
FAKE_CLI = """#!{python}
import json, os, sys, time
from PIL import Image, PngImagePlugin
args = sys.argv[1:]
value = lambda flag: args[args.index(flag) + 1]
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "argv.json"), "w") as f:
    json.dump(args, f)
prompt = value("-p")
print("[INFO ] loading models", flush=True)
if "FAIL" in prompt:
    print("[ERROR] stable-diffusion.cpp:1 - load from file failed", flush=True)
    sys.exit(1)
steps = int(value("--steps"))
if "HANG" in prompt:
    with open(os.path.join(here, "pid"), "w") as f:
        f.write(str(os.getpid()))
    sys.stdout.write("\\r  |=>   | 1/%d - 2.00s/it" % steps)
    sys.stdout.flush()
    while True:
        time.sleep(0.05)
for i in range(1, steps + 1):
    sys.stdout.write("\\r  |=====>  | %d/%d - 2.50s/it\\x1b[K" % (i, steps))
sys.stdout.write("\\n\\r  |=====| 9/9 - 5.00it/s\\n")
sys.stdout.flush()
out = value("-o")
w, h = int(value("-W")), int(value("-H"))
if "NOPICTURE" in prompt:
    sys.exit(0)
if "NOTPNG" in prompt:
    open(out, "w").write("not a picture")
    sys.exit(0)
if "WRONGSIZE" in prompt:
    w = h = 64
info = PngImagePlugin.PngInfo()
info.add_text("parameters", "prompt: " + prompt + " model: /secret/path/model.gguf")
Image.new("RGB", (w, h), (10, 20, 30)).save(out, pnginfo=info)
"""


def fake_cli(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    cli = folder / "sd-cli"
    cli.write_text(FAKE_CLI.replace("{python}", sys.executable), encoding="utf-8")
    cli.chmod(0o755)
    return cli


class FakeInstaller:
    def __init__(self, cli: Path) -> None:
        self.cli = cli
        self.installs = 0

    def size(self, _variant: str) -> int:
        return 0

    async def install(self, _variant: str) -> Path:
        self.installs += 1
        return self.cli


class FakeDownloads:
    def __init__(self, root: Path) -> None:
        self.root = root

    def file_path(self, entry, file) -> Path:  # noqa: ANN001
        return self.root / entry.id / file.path

    def is_downloaded(self, _entry) -> bool:  # noqa: ANN001
        return True


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0  # each reading a second later
        return self.now


def app_sessions(tmp_path: Path):  # noqa: ANN201
    engine = make_engine(tmp_path / "app.sqlite")
    migrate(engine)
    return make_sessionmaker(engine)


def images(tmp_path: Path, runtime, *, timeout: float = 60.0) -> tuple[Images, FakeInstaller]:  # noqa: ANN001
    installer = FakeInstaller(fake_cli(tmp_path / "bin"))
    made = Images(
        runtime=runtime,
        downloads=FakeDownloads(tmp_path / "models"),
        installer=installer,
        variant=lambda: "test",
        sessions=app_sessions(tmp_path),
        run_dir=tmp_path / "run",
        clock=Clock(),
        timeout=timeout,
    )
    return made, installer


def klein(model_id: str = "flux2-klein-4b") -> catalog.CatalogEntry:
    return catalog.load().get(model_id)
