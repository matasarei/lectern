from __future__ import annotations

from pathlib import Path

from lectern.hardware import vulkan
from lectern.hardware.probe import Gpu, probe

FIXTURES = Path(__file__).parent.parent / "fixtures"
MIB = 1024**2

# Same printf as the Mac capture (llama.cpp common/arg.cpp, b11236), with Vulkan devices.
VULKAN_OUTPUT = """load_backend: loaded Vulkan backend
Available devices:
  Vulkan0: AMD Radeon RX 7600 (RADV NAVI33) (8176 MiB, 7900 MiB free)
  Vulkan1: Intel(R) Arc(tm) A380 Graphics (DG2) (6064 MiB, 6000 MiB free)
"""


def test_parses_the_real_b11236_output():
    output = (FIXTURES / "llama_server_list_devices_b11236_macos.txt").read_text()

    devices = vulkan.parse_devices(output)

    assert [(d.name, d.description, d.total // MIB, d.free // MIB) for d in devices] == [
        ("MTL0", "Apple M4 Max", 38338, 38338),
        ("BLAS", "Accelerate", 0, 0),
    ]


def test_descriptions_with_parentheses_keep_them():
    devices = vulkan.parse_devices(VULKAN_OUTPUT)
    assert devices[0].description == "AMD Radeon RX 7600 (RADV NAVI33)"
    assert devices[0].total == 8176 * MIB


def test_only_vulkan_devices_become_gpus():
    real = vulkan.parse_devices(
        (FIXTURES / "llama_server_list_devices_b11236_macos.txt").read_text()
    )
    assert vulkan.gpus_from_devices(real) == []

    gpus = vulkan.gpus_from_devices(vulkan.parse_devices(VULKAN_OUTPUT))

    assert [(g.vendor, g.memory_total // MIB, g.unified) for g in gpus] == [
        ("amd", 8176, False),
        ("intel", 6064, False),
    ]


def test_no_devices_line_gives_nothing():
    assert vulkan.parse_devices("Available devices:\n  (none)\n") == []


def test_probe_asks_vulkan_only_without_nvidia():
    amd = Gpu("amd", "Radeon", 8176 * MIB, 7900 * MIB, unified=False)
    asked = []

    def vulkan_probe():
        asked.append(1)
        return [amd]

    hw = probe(os_name="linux", nvidia_probe=lambda: [], vulkan_probe=vulkan_probe)
    assert hw.gpus == [amd] and asked == [1]

    nv = Gpu("nvidia", "RTX", 8 * 1024 * MIB, 0, unified=False, cuda_driver="13.0")
    asked.clear()
    hw = probe(os_name="linux", nvidia_probe=lambda: [nv], vulkan_probe=vulkan_probe)
    assert hw.gpus == [nv] and asked == []


def test_probe_without_a_vulkan_build_is_cpu_only(paths):
    hw = probe(os_name="linux", nvidia_probe=lambda: [])
    assert hw.gpus == []
