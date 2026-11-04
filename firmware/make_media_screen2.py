"""v2 de la pantalla "MEDIA" (Default Screen = 4): 5 botones, bateria del mouse y play/pause que alterna.

Mismos cambios de base que make_media_screen.py (cmd 0x0A acepta 4, setter acepta indice 3, dispatcher
reconstruye la pagina 1, envoltorio p1_ctor) mas:

  * Panel propio en (0,35) de 320x170: es donde page_root ubica el contenedor de cada pagina (el
    display logico es mas alto que el panel). v1 lo ponia en y=0 y quedaba 35 px corrido.
  * Bateria: los mismos widgets que usan las paginas del menu (0x0801B7A8 / 0x0801BAC8), guardados en
    0x2000163C+0x68/+0x6C; el refresco periodico de la UI (0x08022FEC) los actualiza solo y page_root
    los pone en cero al cambiar de pagina.
  * Fila de 5 botones: vol-, anterior, play/pause, siguiente, vol+. Zona tactil = quinto horizontal
    (x/64) debajo de la franja del titulo. Cada uno manda [04 CD <codigo> 00 ...] al nRF:
      0x10 play/pause, 0x11 siguiente, 0x12 anterior, 0x13 vol+, 0x14 vol-
  * Play/pause alterna el icono (play <-> pausa). No usa RAM propia: busca su imagen entre los hijos de
    la pantalla comparando img->src (offset 0x2C en lv_image_t, ver 0x0805D830).

Solo escribe archivos. No habla con el dongle.
"""
import argparse
import hashlib
import pathlib
import struct
import sys
import zlib

import make_media_button as base
import make_media_screen as v1
from make_media_button import parse_hex, rec, verify
from make_media_screen import Asm2, imm_txt

ROOT = pathlib.Path(__file__).resolve().parent.parent
CODE = 0x08119020
IMG_END = base.IMG_END

F_SPI_SEND = 0x08015A68
F_P1_CTOR = 0x0801F20C
F_IDX_GET = 0x08019F78
F_SCREEN_ACTIVE = 0x0806B474
F_OBJ_CREATE = 0x080640A4      # lv_obj_create(parent)
F_REMOVE_STYLE_ALL = 0x080683B8
F_SET_SIZE = 0x08068D80
F_IMG_CREATE = 0x0805CC48
F_IMG_SET_SRC = 0x0805D748
F_SET_POS = 0x08068CA0
F_TINT = 0x0801B68C
F_REMOVE_FLAG = 0x08067F98
F_LABEL = 0x0801EB48
F_BATT_WIDGET = 0x0801B7A8     # (padre, x, y) -> obj; se guarda en ESTADO+0x68
F_BATT_WIDGET2 = 0x0801BAC8    # (padre, x, y) -> obj; se guarda en ESTADO+0x6C
F_STATUS_UPDATE = 0x08022FEC   # refresca los dos widgets de arriba
MENU_BG_DSC = 0x0808D814
FONT = 0x080A3758
TEXT_COLOR = 0x00F1F0EC
CFG_BYTE1 = 0x200014FD
STATE = 0x2000163C

PAGE_Y = 35                    # contenedor de pagina: (0,35) 320x170
TAP_Y_MIN = PAGE_Y + 40        # debajo de la franja del titulo
CODES = [0x14, 0x12, 0x10, 0x11, 0x13]   # zona 0..4 (x/64)

# (nombre, tamano, centro x) — fila centrada en y=98 del panel
BUTTONS = [("vol_down", 46, 32), ("prev", 50, 96), ("play", 62, 160), ("next", 50, 224), ("vol_up", 46, 288)]
ROW_CY = 98


class Asm3(Asm2):
    COND = {**Asm2.COND, "mi": 4, "pl": 5}

    def lsls_i(self, rd, rm, imm):
        self.h(imm << 6 | rm << 3 | rd, f"lsls r{rd}, r{rm}, {imm_txt(imm)}")

    def lsrs_i(self, rd, rm, imm):
        self.h(0x0800 | imm << 6 | rm << 3 | rd, f"lsrs r{rd}, r{rm}, {imm_txt(imm)}")

    def add_r(self, rdn, rm):
        self.h(0x4400 | (rdn >> 3) << 7 | rm << 3 | (rdn & 7), f"add r{rdn}, r{rm}")

    def ldrh_i(self, rt, rn, imm):
        self.h(0x8800 | (imm // 2) << 6 | rn << 3 | rt, f"ldrh r{rt}, [r{rn}, {imm_txt(imm)}]")

    def ldr_i0(self, rt, rn):
        self.h(0x6800 | rn << 3 | rt, f"ldr r{rt}, [r{rn}]")

    def ldrb_r(self, rt, rn, rm):
        self.h(0x5C00 | rm << 6 | rn << 3 | rt, f"ldrb r{rt}, [r{rn}, r{rm}]")

    def strb_i(self, rt, rn, imm):
        self.h(0x7000 | imm << 6 | rn << 3 | rt, f"strb r{rt}, [r{rn}, {imm_txt(imm)}]")

    def subs_imm8(self, rdn, imm):
        self.h(0x3800 | rdn << 8 | imm, f"subs r{rdn}, {imm_txt(imm)}")

    def b(self, label):
        def gen(a):
            off = (self.labels[label] - (a + 4)) // 2
            assert -1024 <= off < 1024
            return struct.pack("<H", 0xE000 | (off & 0x7FF))
        self._emit(2, gen, lambda: f"b #{self.labels[label]:#x}")

    def bcond_w(self, cond, label):  # B<c>.W T3, +-1 MB
        def gen(a):
            off = self.labels[label] - (a + 4)
            assert -(1 << 20) <= off < (1 << 20) and off % 2 == 0
            s = 1 if off < 0 else 0
            o = off & ((1 << 21) - 1)
            imm11, imm6 = (o >> 1) & 0x7FF, (o >> 12) & 0x3F
            j1, j2 = (o >> 18) & 1, (o >> 19) & 1
            h1 = 0xF000 | s << 10 | self.COND[cond] << 6 | imm6
            h2 = 0x8000 | j1 << 13 | j2 << 11 | imm11
            return struct.pack("<HH", h1, h2)
        self._emit(4, gen, lambda: f"b{cond}.w #{self.labels[label]:#x}")

    def mov32(self, rd, value):
        self.movw(rd, value & 0xFFFF)
        self.movt(rd, value >> 16)

    def addr(self, rd, label):
        self.movw_label(rd, label, 0)
        self.movw_label(rd, label, 1)


# ------------------------------------------------------------------ iconos (A8, se tinen con el tema)
def _tri(p, a, b, c):
    def side(p1, p2, q):
        return (p2[0] - p1[0]) * (q[1] - p1[1]) - (p2[1] - p1[1]) * (q[0] - p1[0])
    d1, d2, d3 = side(a, b, p), side(b, c, p), side(c, a, p)
    neg = d1 < 0 or d2 < 0 or d3 < 0
    pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (neg and pos)


def _rect(p, x0, x1, y0, y1):
    return x0 <= p[0] <= x1 and y0 <= p[1] <= y1


def glyph(name, u, v):
    p = (u, v)
    if name == "play":
        return _tri(p, (0.39, 0.28), (0.39, 0.72), (0.74, 0.50))
    if name == "pause":
        return _rect(p, 0.33, 0.45, 0.29, 0.71) or _rect(p, 0.55, 0.67, 0.29, 0.71)
    if name == "next":
        return (_tri(p, (0.25, 0.31), (0.25, 0.69), (0.47, 0.50)) or _tri(p, (0.46, 0.31), (0.46, 0.69), (0.68, 0.50))
                or _rect(p, 0.68, 0.76, 0.31, 0.69))
    if name == "prev":
        return glyph("next", 1.0 - u, v)
    if name in ("vol_down", "vol_up"):
        spk = _rect(p, 0.20, 0.32, 0.41, 0.59) or _tri(p, (0.31, 0.41), (0.47, 0.26), (0.47, 0.74)) \
            or _tri(p, (0.31, 0.41), (0.47, 0.74), (0.31, 0.59))
        minus = _rect(p, 0.55, 0.81, 0.46, 0.54)
        plus = _rect(p, 0.64, 0.72, 0.37, 0.63)
        return spk or minus or (name == "vol_up" and plus)
    raise ValueError(name)


def make_icon(name, size):
    S = 4
    out = bytearray()
    r = 0.47
    for py in range(size):
        for px in range(size):
            acc = 0
            for sy in range(S):
                for sx in range(S):
                    u = (px + (sx + 0.5) / S) / size
                    v = (py + (sy + 0.5) / S) / size
                    if (u - 0.5) ** 2 + (v - 0.5) ** 2 <= r * r and not glyph(name, u, v):
                        acc += 1
            out.append(round(235 * acc / (S * S)))
    return bytes(out)


def write_panel_preview(icons, path):
    """Vista previa del panel 320x170 (fondo oscuro, iconos violeta; el titulo se dibuja como barra)."""
    W, H, sc = 320, 170, 2
    img = [[(24, 22, 30) for _ in range(W)] for _ in range(H)]
    fg = (0xB7, 0x65, 0xFF)
    for name, size, cx in BUTTONS:
        a = icons[name]
        x0, y0 = cx - size // 2, ROW_CY - size // 2
        for y in range(size):
            for x in range(size):
                al = a[y * size + x] / 255
                bg = img[y0 + y][x0 + x]
                img[y0 + y][x0 + x] = tuple(round(fg[c] * al + bg[c] * (1 - al)) for c in range(3))
    for y in range(10, 26):                      # "MEDIA" (aprox.)
        for x in range(136, 184):
            img[y][x] = (241, 240, 236)
    for y in range(5, 31):                       # bateria (aprox.)
        for x in range(299, 316):
            if x in (299, 315) or y in (5, 30) or y > 14:
                img[y][x] = (241, 240, 236)
    rows = []
    for y in range(H * sc):
        row = bytearray([0])
        for x in range(W * sc):
            row += bytes(img[y // sc][x // sc])
        rows.append(bytes(row))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W * sc, H * sc, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


# ------------------------------------------------------------------ codigo
def build():
    A = Asm3(CODE)

    # hook_tap: r0=y r1=0 r2=x r3=pagina r5=estado r7=1<<pagina; r6 libre (el original lo pisa)
    A.label("hook_tap")
    A.cmp_i(3, 1)
    A.bcond("ne", "tap_orig")
    A.mov32(6, CFG_BYTE1)
    A.ldrb_i(6, 6, 0)
    A.and_imm8(6, 6, 3)
    A.cmp_i(6, 3)
    A.bcond("ne", "tap_orig")
    A.mov_r(6, 0)
    A.subs_imm8(6, TAP_Y_MIN)
    A.cmp_i(6, 204 - TAP_Y_MIN)
    A.bcond("hi", "tap_orig")
    A.movw(6, 319)
    A.cmp_r(2, 6)
    A.bcond("hi", "tap_orig")
    A.lsrs_i(6, 2, 6)                 # zona = x / 64
    A.sub_sp(8)
    A.addr(1, "codes")
    A.ldrb_r(1, 1, 6)
    A.movw(0, 0xCD04)
    A.str_sp(0, 0)
    A.movs(0, 0)
    A.str_sp(0, 4)
    A.add_rd_sp(0, 0)
    A.strb_i(1, 0, 2)
    A.movs(1, 8)
    A.bl(F_SPI_SEND)
    A.add_sp(8)
    A.cmp_i(6, 2)
    A.bcond("ne", "tap_done")
    A.bl("toggle_pp")
    A.label("tap_done")
    A.add_sp(0x6C)
    A.pop([4, 5, 6, 7, "pc"], "pop {r4, r5, r6, r7, pc}")
    A.label("tap_orig")
    A.movw(6, 0x806)
    A.bw(0x0801E57C)

    # hook_map: cmd 0x0A, valor 4 -> indice 3
    A.label("hook_map")
    A.cmp_i(0, 4)
    A.bcond("ne", "hm_orig")
    A.movs(1, 3)
    A.bw(0x08024A5E)
    A.label("hm_orig")
    A.raw("22fa01f1", "lsr.w r1, r2, r1")
    A.bw(0x08024A5E)

    # p1_ctor: Mouse Stats original + panel si el indice es 3
    A.label("p1_ctor")
    A.push([4, "lr"], "push {r4, lr}")
    A.bl(F_P1_CTOR)
    A.mov_r(4, 0)
    A.bl(F_IDX_GET)
    A.cmp_i(0, 3)
    A.bcond("ne", "p1_done")
    A.bl(F_SCREEN_ACTIVE)
    A.bl("add_panel")
    A.label("p1_done")
    A.mov_r(0, 4)
    A.pop([4, "pc"], "pop {r4, pc}")

    # add_panel(r0 = pantalla)
    A.label("add_panel")
    A.push([4, 5, 6, "lr"], "push {r4, r5, r6, lr}")
    A.sub_sp(0x18)
    A.cmp_i(0, 0)
    A.bcond_w("eq", "ap_done")
    A.bl(F_OBJ_CREATE)
    A.cmp_i(0, 0)
    A.bcond_w("eq", "ap_done")
    A.mov_r(5, 0)
    A.bl(F_REMOVE_STYLE_ALL)
    A.mov_r(0, 5)
    A.movs(1, 0)
    A.movs(2, PAGE_Y)
    A.bl(F_SET_POS)
    A.mov_r(0, 5)
    A.movw(1, 320)
    A.movs(2, 170)
    A.bl(F_SET_SIZE)
    A.mov_r(0, 5)
    A.movs(1, 0x12)
    A.bl(F_REMOVE_FLAG)
    # fondo
    A.mov_r(0, 5)
    A.bl(F_IMG_CREATE)
    A.cmp_i(0, 0)
    A.bcond_w("eq", "ap_done")
    A.mov_r(4, 0)
    A.mov32(1, MENU_BG_DSC)
    A.bl(F_IMG_SET_SRC)
    A.mov_r(0, 4)
    A.movs(1, 0)
    A.movs(2, 0)
    A.bl(F_SET_POS)
    A.mov_r(0, 4)
    A.movs(1, 0x12)
    A.bl(F_REMOVE_FLAG)
    # bateria (mismos widgets y posicion que las paginas del menu)
    A.mov_r(0, 5)
    A.movw(1, 299)
    A.movs(2, 5)
    A.bl(F_BATT_WIDGET)
    A.mov32(1, STATE)
    A.str_i(0, 1, 0x68)
    A.mov_r(0, 5)
    A.movw(1, 302)
    A.movs(2, 5)
    A.bl(F_BATT_WIDGET2)
    A.mov32(1, STATE)
    A.str_i(0, 1, 0x6C)
    # titulo
    A.movs(0, 0)
    A.str_sp(0, 0)
    A.movs(0, 6)
    A.str_sp(0, 4)
    A.movw(0, 320)
    A.str_sp(0, 8)
    A.movs(0, 26)
    A.str_sp(0, 12)
    A.movs(0, 2)
    A.str_sp(0, 16)
    A.mov_r(0, 5)
    A.addr(1, "title_txt")
    A.mov32(2, FONT)
    A.mov32(3, TEXT_COLOR)
    A.bl(F_LABEL)
    # botones
    for name, size, cx in BUTTONS:
        A.mov_r(0, 5)
        A.addr(1, f"dsc_{name}")
        A.movw(2, cx - size // 2)
        A.movs(3, ROW_CY - size // 2)
        A.bl("put_icon")
    A.bl(F_STATUS_UPDATE)
    A.label("ap_done")
    A.add_sp(0x18)
    A.pop([4, 5, 6, "pc"], "pop {r4, r5, r6, pc}")

    # put_icon(r0=padre, r1=dsc, r2=x, r3=y)
    A.label("put_icon")
    A.push([4, 5, 6, 7, "lr"], "push {r4, r5, r6, r7, lr}")
    A.sub_sp(4)
    A.mov_r(4, 1)
    A.mov_r(5, 2)
    A.mov_r(6, 3)
    A.bl(F_IMG_CREATE)
    A.cmp_i(0, 0)
    A.bcond("eq", "pi_done")
    A.mov_r(7, 0)
    A.mov_r(1, 4)
    A.bl(F_IMG_SET_SRC)
    A.mov_r(0, 7)
    A.mov_r(1, 5)
    A.mov_r(2, 6)
    A.bl(F_SET_POS)
    A.mov_r(0, 7)
    A.bl(F_TINT)
    A.mov_r(0, 7)
    A.movs(1, 0x12)
    A.bl(F_REMOVE_FLAG)
    A.label("pi_done")
    A.add_sp(4)
    A.pop([4, 5, 6, 7, "pc"], "pop {r4, r5, r6, r7, pc}")

    # toggle_pp: busca entre los hijos de los hijos de la pantalla la imagen con src play/pausa y la invierte
    A.label("toggle_pp")
    A.push([4, 5, 6, 7, "lr"], "push {r4, r5, r6, r7, lr}")
    A.sub_sp(4)
    A.addr(5, "dsc_play")
    A.addr(6, "dsc_pause")
    A.bl(F_SCREEN_ACTIVE)
    A.cmp_i(0, 0)
    A.bcond("eq", "tp_done")
    A.ldr_i(1, 0, 8)                 # spec_attr
    A.cmp_i(1, 0)
    A.bcond("eq", "tp_done")
    A.ldrh_i(2, 1, 0x28)             # child_cnt
    A.ldr_i0(1, 1)                   # children
    A.label("tp_outer")
    A.subs_imm8(2, 1)
    A.bcond("mi", "tp_done")
    A.lsls_i(3, 2, 2)
    A.add_r(3, 1)
    A.ldr_i0(3, 3)                   # hijo c
    A.ldr_i(3, 3, 8)                 # c->spec_attr
    A.cmp_i(3, 0)
    A.bcond("eq", "tp_outer")
    A.ldrh_i(4, 3, 0x28)
    A.ldr_i0(3, 3)
    A.label("tp_inner")
    A.subs_imm8(4, 1)
    A.bcond("mi", "tp_outer")
    A.lsls_i(0, 4, 2)
    A.add_r(0, 3)
    A.ldr_i0(7, 0)                   # objeto
    A.ldr_i(0, 7, 0x2C)              # img->src
    A.cmp_r(0, 5)
    A.bcond("eq", "tp_to_pause")
    A.cmp_r(0, 6)
    A.bcond("ne", "tp_inner")
    A.mov_r(1, 5)
    A.b("tp_set")
    A.label("tp_to_pause")
    A.mov_r(1, 6)
    A.label("tp_set")
    A.mov_r(0, 7)
    A.bl(F_IMG_SET_SRC)
    A.label("tp_done")
    A.add_sp(4)
    A.pop([4, 5, 6, 7, "pc"], "pop {r4, r5, r6, r7, pc}")

    # ---- datos
    code_end = A.pc
    p = (code_end + 3) & ~3
    data = bytearray()
    layout = {}

    def place(name, blob, align=4):
        nonlocal p
        p = (p + align - 1) & ~(align - 1)
        layout[name] = (p, blob)
        p += len(blob)

    place("title_txt", b"MEDIA\x00")
    place("codes", bytes(CODES))
    icons = {}
    for name, size in [("play", 62), ("pause", 62)] + [(n, s) for n, s, _ in BUTTONS if n != "play"]:
        icons[name] = make_icon(name, size)
    sizes = {"play": 62, "pause": 62, **{n: s for n, s, _ in BUTTONS}}
    for name in icons:
        place(f"dsc_{name}", b"\x00" * 24)
        place(f"data_{name}", icons[name])
    for name in icons:
        s = sizes[name]
        layout[f"dsc_{name}"] = (layout[f"dsc_{name}"][0],
                                 struct.pack("<BBHHHHHIII", 0x19, 0x0E, 0, s, s, s, 0, s * s, layout[f"data_{name}"][0], 0))
    for name, (addr, _) in layout.items():
        A.labels[name] = addr

    code, listing = A.assemble()
    verify(listing)
    blob = bytearray(code)
    for name, (addr, b) in sorted(layout.items(), key=lambda kv: kv[1][0]):
        blob += b"\x00" * (addr - (CODE + len(blob)))
        blob += b
    return A, bytes(blob), listing, icons


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persist", action="store_true")
    args = ap.parse_args()
    out_dir = ROOT / ("patched-screen2-persist" if args.persist else "patched-screen2")
    dst = out_dir / base.SRC.name

    raw = base.SRC.read_bytes()
    if hashlib.sha256(raw).hexdigest() != base.SRC_SHA:
        sys.exit("el hex de partida no es el parcheado esperado")
    lines = raw.decode("ascii").splitlines(keepends=True)
    recs = parse_hex(lines)
    mem = {}
    for _, start, b in recs:
        for k, x in enumerate(b[4:4 + b[0]]):
            mem[start + k] = x
    orig = dict(mem)

    A, blob, listing, icons = build()
    print(f"codigo + datos: {CODE:#x}..{CODE + len(blob):#x} ({len(blob)} bytes)")
    for a, b, t in listing:
        print(f"  {a:08X}: {b.hex():8}  {t}")
    v1.apply(mem, A, args.persist)

    for idx, start, b in recs:
        rb = bytearray(b)
        if any(mem[start + k] != b[4 + k] for k in range(b[0])):
            for k in range(b[0]):
                rb[4 + k] = mem[start + k]
            rb[-1] = (-sum(rb[:-1])) & 0xFF
            eol = lines[idx][len(lines[idx].rstrip("\r\n")):]
            lines[idx] = ":" + rb.hex().upper() + eol
    assert (CODE >> 16) == ((CODE + len(blob)) >> 16), "el bloque cruza un limite de 64 KB"
    last_idx = recs[-1][0]
    lines[last_idx + 1:last_idx + 1] = [rec((CODE + i) & 0xFFFF, blob[i:i + 16]) for i in range(0, len(blob), 16)]
    out = "".join(lines).encode("ascii")
    out_dir.mkdir(exist_ok=True)
    dst.write_bytes(out)
    write_panel_preview(icons, out_dir / "panel_preview.png")

    m2 = {}
    for _, start, b in parse_hex(out.decode("ascii").splitlines()):
        for k, x in enumerate(b[4:4 + b[0]]):
            m2[start + k] = x
    for line in out.decode("ascii").splitlines():
        assert sum(bytes.fromhex(line[1:])) & 0xFF == 0
    assert all(m2[a] == mem[a] for a in mem)
    assert all(m2[CODE + i] == x for i, x in enumerate(blob))
    print("bytes cambiados dentro de la imagen original:", sum(1 for a in orig if orig[a] != m2[a]))
    print("escrito:", dst)
    print("SHA256:", hashlib.sha256(out).hexdigest())


if __name__ == "__main__":
    main()
