# Firmware patch generator

These scripts build the patched screen firmware (MH2435, receiver v0.0.15.0) from the official one
and produce [`../src/patch_v6.json`](../src/patch_v6.json), which is what the app applies.
The comments are in Spanish (that's how the project started).

Nothing here is needed to *use* WL Dongle Media: it's for people who want to audit or change the patch.

## Rebuilding the patch

1. Put the official hex in `backup-original/` (repo root). You can download it from WLMouse's server:
   `https://gm.wlmouse.gg/Config/fwfiles/WL_X_V2/WL_BEAST_X_V2_8K_Dongle_MH2435_App_v0.0.15.0_20260927.hex`
   and save it as `backup-original/WL_BEAST_X_V2_8K_Dongle_MH2435_App_v0.0.15.0_20260927-BY1FO-J5.hex`
   (SHA-256 `24acb4582059af98411025d27cbdf2d9c538d9556b016c244303ddcdeb2d226f`).
2. `pip install capstone`
3. Run, from this folder:
   ```
   python make_patch.py                              # double-tap -> play/pause (base for the rest)
   python make_media_screen6.py --persist --english  # Media Control screens
   python make_release_patch.py                      # writes ../src/patch_v6.json
   ```
   The scripts check the SHA-256 of their inputs and outputs, so you get byte-identical results.

## What the scripts do

- `make_media_button.py` … `make_media_screen6.py`: successive versions. Each one builds on the
  previous one's helpers: a tiny Thumb-2 assembler (`Asm*`) whose every instruction is verified by
  disassembling it with Capstone, plus the addresses of the stock LVGL functions it calls.
- New code and data go in free flash right after the stock image (`0x08119020`). The stock code is
  only touched at a handful of single-instruction hook sites; the script refuses any hook where the
  firmware could branch into the middle of a patched instruction.
- `make_release_patch.py` diffs the official image against the patched one and keeps only the
  changed bytes.
- `font_metrics.py` extracts the advance widths of the dongle's 18 px font (`../src/font18.json`),
  so the PC can center and fit texts exactly.
