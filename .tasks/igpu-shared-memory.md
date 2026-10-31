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

## Notes (2026-10-31)
- Flipping `unified=True` alone breaks it: `gpu_budget` then returns `apple_budget or 0`, i.e. 0
  off a Mac, and everything runs on the CPU. A shared GPU needs its own budget: the smaller of
  what it may borrow and what the system can spare (free RAM less a margin; on Windows
  `commit_available` already reads what's left).
- On shared memory, spilling layers to the CPU (the planner's last step) saves no memory: the
  GPU and the CPU use the same RAM. The whole model counts against what the system can spare.

## Decided (2026-10-31, the developer)
- Running on the integrated GPU because the discrete one is gone (a laptop on battery) is an
  exception, not a setup to plan for: no tier change, no suggesting or switching to a smaller
  model.
- When a model doesn't fit in what the system can spare, say plainly what's wrong (how much it
  needs, how much is free, that the GPU shares system RAM) and ask the person to free RAM
  (close apps) and try again. That's all.
