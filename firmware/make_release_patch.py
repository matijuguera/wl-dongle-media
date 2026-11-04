"""Genera `patch_v6.json`: solo los bytes NUESTROS (diferencias contra el firmware oficial).

El release no lleva el firmware de WLMouse: el instalador baja el oficial, comprueba su SHA-256 y le
aplica este parche. Se arma a partir de backup-original/ y patched-screen6-en-persist/ (titulos en ingles).

    python make_release_patch.py
"""
import hashlib
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from flasher import load_hex  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
NAME = "WL_BEAST_X_V2_8K_Dongle_MH2435_App_v0.0.15.0_20260927-BY1FO-J5.hex"
OUT = ROOT / "src" / "patch_v6.json"


def sha(b):
    return hashlib.sha256(b).hexdigest()


def runs(old, new):
    """Tramos [offset, bytes] donde `new` difiere de `old` (lo que sobra al final cuenta entero)."""
    out, i = [], 0
    while i < len(new):
        if i < len(old) and old[i] == new[i]:
            i += 1
            continue
        j = i
        # junta diferencias separadas por menos de 8 bytes iguales
        while j < len(new) and (j >= len(old) or old[j] != new[j] or
                                any(k < len(new) and (k >= len(old) or old[k] != new[k]) for k in range(j, j + 8))):
            j += 1
        out.append([i, new[i:j].hex()])
        i = j
    return out


def main():
    hex_text = (ROOT / "backup-original" / NAME).read_bytes()
    old, base = load_hex(hex_text.decode("ascii"))
    new, base2 = load_hex((ROOT / "patched-screen6-en-persist" / NAME).read_text(encoding="ascii", newline=""))
    assert base == base2
    patch = {
        "name": "Media Control v6",
        "official_url": "https://gm.wlmouse.gg/Config/fwfiles/WL_X_V2/WL_BEAST_X_V2_8K_Dongle_MH2435_App_v0.0.15.0_20260927.hex",
        "official_hex_sha256": sha(hex_text),
        "official_image_sha256": sha(old),
        "base": base,
        "size": len(new),
        "result_sha256": sha(new),
        "runs": runs(old, new),
    }
    OUT.write_text(json.dumps(patch, indent=1), encoding="utf-8")
    ours = sum(len(r[1]) // 2 for r in patch["runs"])
    print(f"{OUT.name}: {len(patch['runs'])} tramos, {ours} bytes propios de {len(new)}")


if __name__ == "__main__":
    main()
