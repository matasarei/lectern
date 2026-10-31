# An integrated GPU's memory is counted as its own

**Type:** bug
**Language:** en
**Asked:** found on the M4 re-run, 2026-10-31 (Windows laptop on battery: Intel Arc, Vulkan)

## Seen
`hardware/vulkan.py` `gpus_from_devices` reports every Vulkan GPU with `unified=False`. The Arc
has no memory of its own: its "8293 MiB free" is shared system RAM (half of 16 GB). The runtime
said `"unified": false, "budget": 8791890330` and loaded the 9B + PaddleOCR-VL (6.9 GB) out of
the same RAM Windows and the apps use, with no margin kept for them. Linux with an iGPU on
Vulkan would do the same.

## To do
- Tell an integrated GPU from a discrete one (Vulkan device type, or the vendor/name), mark it
  `unified=True`, and budget it like Apple's unified memory (`probe.py`, `planner.py`).
- Test in `tests/hardware/`.
