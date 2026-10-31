"""Does a thread blocked reading stdin (a pipe) stop `import <module>` on Windows?

Run as a child with stdin a pipe that stays open: `--reader` starts a thread reading stdin,
as `lectern serve --stop-on-stdin-close` does; then the module is imported and timed.
"""

import subprocess
import sys
import threading
import time


def child(module: str, reader: bool) -> None:
    if reader:
        threading.Thread(target=lambda: sys.stdin.buffer.read(4096), daemon=True).start()
        time.sleep(0.5)  # let it block in read()
    start = time.monotonic()
    __import__(module)
    print(f"imported {module} in {time.monotonic() - start:.1f} s", flush=True)


def main() -> None:
    if sys.argv[1] == "child":
        child(sys.argv[2], sys.argv[3] == "1")
        return
    for module in sys.argv[1:]:
        for reader in (False, True):
            proc = subprocess.Popen(
                [sys.executable, "-I", __file__, "child", module, str(int(reader))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            # Not communicate(): it would close stdin at once. stdin stays open, as the tray's does.
            lines: list[str] = []
            waiter = threading.Thread(target=lambda: lines.append(proc.stdout.readline()))
            waiter.start()
            waiter.join(20)
            if lines:
                result = lines[0].strip()
            else:
                proc.stdin.close()  # what Quit does: the read returns
                waiter.join(20)
                result = f"HUNG 20 s; after stdin closed: {''.join(lines).strip()}"
            proc.wait(20)
            print(f"{module:10} reader={reader!s:5}  {result}", flush=True)


if __name__ == "__main__":
    main()
