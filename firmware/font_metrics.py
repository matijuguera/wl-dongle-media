"""Extrae del firmware MH2435 el ancho de avance de cada caracter de una fuente LVGL (lv_font_fmt_txt).

La PC lo usa para calcular cuanto mide un texto en la pantalla del dongle (ej. para ubicar el logo de
la app al lado del nombre centrado, o el ancho de la pildora de la salida de audio).

    python font_metrics.py            # regenera font18.json desde backup-original/
"""
import json
import pathlib
import struct

ROOT = pathlib.Path(__file__).resolve().parent.parent
HEX = ROOT / "backup-original" / "WL_BEAST_X_V2_8K_Dongle_MH2435_App_v0.0.15.0_20260927-BY1FO-J5.hex"
OUT = pathlib.Path(__file__).with_name("font18.json")
FONT_18 = 0x080A3710


def load_hex(path):
    mem, base = {}, 0
    for line in path.read_text().splitlines():
        if not line.startswith(":"):
            continue
        b = bytes.fromhex(line[1:])
        n, off, typ = b[0], (b[1] << 8) | b[2], b[3]
        if typ == 0:
            for i, x in enumerate(b[4:4 + n]):
                mem[base + off + i] = x
        elif typ == 4:
            base = int.from_bytes(b[4:6], "big") << 16
    return mem


def extract(font_addr):
    mem = load_hex(HEX)
    rd = lambda a, n: bytes(mem[a + i] for i in range(n))
    u32 = lambda a: struct.unpack("<I", rd(a, 4))[0]
    u16 = lambda a: struct.unpack("<H", rd(a, 2))[0]
    line_height = u32(font_addr + 12)
    dsc = u32(font_addr + 0x18)
    glyph_dsc, cmaps = u32(dsc + 4), u32(dsc + 8)
    cmap_num = u16(dsc + 18) & 0x1FF
    widths = {}
    for k in range(cmap_num):
        a = cmaps + k * 20
        rs, rl, gid0 = u32(a), u16(a + 4), u16(a + 6)
        ulist, ll, typ = u32(a + 8), u16(a + 16), mem[a + 18]
        if typ in (0, 2):  # rango continuo
            pairs = [(rs + i, gid0 + i) for i in range(rl)]
        else:              # lista dispersa
            pairs = [(rs + u16(ulist + 2 * j), gid0 + j) for j in range(ll)]
        for cp, gid in pairs:
            w = u32(glyph_dsc + gid * 8)
            widths[cp] = ((w >> 20) & 0xFFF) / 16.0   # adv_w en 1/16 px
    return {"line_height": line_height, "adv": {str(k): v for k, v in sorted(widths.items())}}


if __name__ == "__main__":
    data = extract(FONT_18)
    OUT.write_text(json.dumps(data))
    adv = {int(k): v for k, v in data["adv"].items()}
    sample = "Kiwi Ears-Allegro PRO"
    print("alto de linea:", data["line_height"], "| glifos:", len(adv))
    print("ancho de", repr(sample), "=", round(sum(adv[ord(c)] for c in sample), 1), "px")
    print("simbolos: musica", adv.get(0xF001), "parlante", adv.get(0xF028), "flechas", adv.get(0xF053), adv.get(0xF054))
