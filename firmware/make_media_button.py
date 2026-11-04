"""Genera el firmware MH2435 con boton Play/Pause en la pantalla Media.

Parte del hex ya parcheado (doble toque -> 04 CD) y agrega:
  * Un icono A8 de 48x48 (circulo con el simbolo play/pause calado) y su lv_image_dsc_t.
  * media_ctor: envoltorio del constructor de la pagina 2 (Media). Llama al original y,
    si no existe la capa decorativa, cuelga el boton de la pantalla activa.
  * hook_overlay: al final de 0x08022BC0 (crea la capa decorativa de la pagina 2) cuelga el
    boton de esa capa, asi queda siempre arriba y se borra junto con ella.
  * hook_tap: en el manejador de toques (0x0801E578). En la pagina 2, un toque con x >= 230
    manda [04 CD 02 00 00 00 00 00] al nRF (igual que el doble toque parcheado) en vez de abrir
    el menu. La PC (dongle_media.py) lo convierte en Play/Pause.

Todo el codigo nuevo va despues del final de la imagen original (0x0811901C). Cada instruccion
se codifica a mano y se verifica desensamblandola con capstone. Solo escribe archivos.
"""
import hashlib
import pathlib
import re
import struct
import sys
import zlib

import capstone

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "patched" / "WL_BEAST_X_V2_8K_Dongle_MH2435_App_v0.0.15.0_20260927-BY1FO-J5.hex"
DST = ROOT / "patched-media" / SRC.name
PREVIEW = ROOT / "patched-media" / "icono_preview.png"
SRC_SHA = "3c9615a1715a8b12c5d7116e58991b0e34d8b99b115267cb2a7859880062bcab"

IMG_END = 0x0811901C
CODE = 0x08119020

# Funciones del firmware (ver README / analisis)
F_SPI_SEND = 0x08015A68      # encola mensaje al nRF (buf, len)
F_MEDIA_CTOR = 0x0801F100    # constructor original de la pagina 2
F_SCREEN_ACTIVE = 0x0806B474  # lv_screen_active()
F_IMG_CREATE = 0x0805CC48    # lv_image_create(parent)
F_IMG_SET_SRC = 0x0805D748   # lv_image_set_src(obj, src)
F_SET_POS = 0x08068CA0       # lv_obj_set_pos(obj, x, y)
F_TINT = 0x0801B68C          # helper del firmware: pinta iconos A8 con el color del tema
F_REMOVE_FLAG = 0x08067F98   # lv_obj_remove_flag(obj, flags)
OVERLAY_PTR = 0x200016B8     # [+4] = capa decorativa de la pagina 2

BTN_X, BTN_Y, ICON = 262, 61, 48
TAP_X_MIN = 230

# Sitios que se modifican: direccion -> (bytes originales esperados, descripcion)
# Cada sitio es UNA instruccion de 32 bits (nadie puede saltar a su mitad); check_no_branch_into() lo verifica.
SITES = {
    0x0801E578: ("40f60606", "movw r6, #0x806 -> b.w hook_tap", "b.w"),
    0x08022C10: ("45f0c2f9", "bl 0x8067f98 (remove_flag de la capa) -> bl hook_overlay", "bl"),
    0x08021758: ("fdf7d2bc", "b.w 0x801f100 -> b.w media_ctor", "b.w"),
    0x0802229A: ("fcf731bf", "b.w 0x801f100 -> b.w media_ctor", "b.w"),
    0x08022BAC: ("fcf7a8ba", "b.w 0x801f100 -> b.w media_ctor", "b.w"),
}


# ---------------------------------------------------------------- mini ensamblador Thumb-2
def imm_txt(v):
    return f"#{v}" if 0 <= v <= 9 else f"#{v:#x}"


class Asm:
    COND = {"eq": 0, "ne": 1, "hs": 2, "lo": 3, "hi": 8, "ls": 9, "ge": 10, "lt": 11, "gt": 12, "le": 13}

    def __init__(self, base):
        self.base = base
        self.items = []  # (addr, bytes|callable, texto esperado)
        self.labels = {}
        self.pc = base

    def label(self, name):
        self.labels[name] = self.pc

    def _emit(self, size, gen, text):
        self.items.append((self.pc, size, gen, text))
        self.pc += size

    def h(self, v, text):
        self._emit(2, lambda a: struct.pack("<H", v), text)

    # 16 bits
    def push(self, regs, text):  # regs: lista de r0-r7, 'lr'
        v = 0xB400 | sum(1 << r for r in regs if r != "lr") | (0x100 if "lr" in regs else 0)
        self.h(v, text)

    def pop(self, regs, text):
        v = 0xBC00 | sum(1 << r for r in regs if r != "pc") | (0x100 if "pc" in regs else 0)
        self.h(v, text)

    def movs(self, rd, imm):
        self.h(0x2000 | rd << 8 | imm, f"movs r{rd}, {imm_txt(imm)}")

    def cmp_i(self, rn, imm):
        self.h(0x2800 | rn << 8 | imm, f"cmp r{rn}, {imm_txt(imm)}")

    def cmp_r(self, rn, rm):
        self.h(0x4280 | rm << 3 | rn, f"cmp r{rn}, r{rm}")

    def mov_r(self, rd, rm):
        self.h(0x4600 | (rd >> 3) << 7 | rm << 3 | (rd & 7), f"mov r{rd}, r{rm}")

    def subs_i8(self, rd, imm):
        self.h(0x3800 | rd << 8 | imm, f"subs r{rd}, {imm_txt(imm)}")

    def sub_sp(self, imm):
        self.h(0xB080 | imm // 4, f"sub sp, #{imm}" if imm < 10 else f"sub sp, #{imm:#x}")

    def add_sp(self, imm):
        self.h(0xB000 | imm // 4, f"add sp, #{imm}" if imm < 10 else f"add sp, #{imm:#x}")

    def add_rd_sp(self, rd, imm):
        self.h(0xA800 | rd << 8 | imm // 4, f"add r{rd}, sp, {imm_txt(imm)}")

    def str_sp(self, rt, imm):
        self.h(0x9000 | rt << 8 | imm // 4, f"str r{rt}, [sp]" if imm == 0 else f"str r{rt}, [sp, {imm_txt(imm)}]")

    def ldr_i(self, rt, rn, imm):
        self.h(0x6800 | (imm // 4) << 6 | rn << 3 | rt, f"ldr r{rt}, [r{rn}, {imm_txt(imm)}]")

    def str_i(self, rt, rn, imm):
        self.h(0x6000 | (imm // 4) << 6 | rn << 3 | rt, f"str r{rt}, [r{rn}, {imm_txt(imm)}]")

    def bcond(self, cond, label):
        def gen(a):
            off = (self.labels[label] - (a + 4)) // 2
            assert -128 <= off < 128
            return struct.pack("<H", 0xD000 | self.COND[cond] << 8 | (off & 0xFF))
        self._emit(2, gen, lambda: f"b{cond} #{self.labels[label]:#x}")

    # 32 bits
    def _movwt(self, rd, imm, top):
        i, imm4, imm3, imm8 = (imm >> 11) & 1, (imm >> 12) & 0xF, (imm >> 8) & 7, imm & 0xFF
        h1 = 0xF240 | top << 7 | i << 10 | imm4
        h2 = imm3 << 12 | rd << 8 | imm8
        return struct.pack("<HH", h1, h2)

    def movw(self, rd, imm):
        self._emit(4, lambda a: self._movwt(rd, imm, 0), f"movw r{rd}, {imm_txt(imm)}")

    def movt(self, rd, imm):
        self._emit(4, lambda a: self._movwt(rd, imm, 1), f"movt r{rd}, {imm_txt(imm)}")

    def movw_label(self, rd, label, top):
        def gen(a):
            v = self.labels[label]
            return self._movwt(rd, (v >> 16) if top else (v & 0xFFFF), top)
        txt = (lambda: f"movt r{rd}, {imm_txt(self.labels[label] >> 16)}") if top else \
              (lambda: f"movw r{rd}, {imm_txt(self.labels[label] & 0xFFFF)}")
        self._emit(4, gen, txt)

    @staticmethod
    def branch32(a, target, link):
        off = target - (a + 4)
        assert -(1 << 24) <= off < (1 << 24) and off % 2 == 0
        s = 1 if off < 0 else 0
        off &= (1 << 25) - 1
        imm11, imm10 = (off >> 1) & 0x7FF, (off >> 12) & 0x3FF
        i1, i2 = (off >> 23) & 1, (off >> 22) & 1
        j1, j2 = (1 - i1) ^ s, (1 - i2) ^ s
        h1 = 0xF000 | s << 10 | imm10
        h2 = (0xD000 if link else 0x9000) | j1 << 13 | j2 << 11 | imm11
        return struct.pack("<HH", h1, h2)

    def bl(self, target):
        t = target if isinstance(target, int) else None
        self._emit(4, lambda a: self.branch32(a, t if t is not None else self.labels[target], True),
                   lambda: f"bl #{(t if t is not None else self.labels[target]):#x}")

    def bw(self, target):
        self._emit(4, lambda a: self.branch32(a, target, False), f"b.w #{target:#x}")

    def assemble(self):
        out = bytearray()
        listing = []
        for a, size, gen, text in self.items:
            b = gen(a)
            assert len(b) == size
            out += b
            listing.append((a, b, text() if callable(text) else text))
        return bytes(out), listing


def verify(listing):
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    for a, b, text in listing:
        ins = list(md.disasm(b, a))
        assert len(ins) == 1, (hex(a), b.hex())
        got = f"{ins[0].mnemonic} {ins[0].op_str}".strip()
        want = text.strip()
        if got != want:
            sys.exit(f"MISMATCH en {a:#x}: codifique '{want}' pero capstone lee '{got}' ({b.hex()})")


# ---------------------------------------------------------------- icono
def make_icon():
    """48x48 A8: disco solido con el simbolo play/pause calado (transparente), suavizado x4."""
    S = 4
    n = ICON * S
    cx = cy = n / 2
    r = 22 * S

    def inside_glyph(x, y):  # coordenadas en pixeles de 48x48
        # triangulo de play: vertices (11,14) (11,34) (23,24)
        if 11 <= x <= 23 and 14 <= y <= 34:
            t = (x - 11) / 12.0
            half = 10 * (1 - t)
            if abs(y - 24) <= half:
                return True
        # dos barras de pausa, bien separadas
        return (26.5 <= x <= 30 or 33.5 <= x <= 37) and 14 <= y <= 34

    data = bytearray()
    for py in range(ICON):
        for px in range(ICON):
            acc = 0
            for sy in range(S):
                for sx in range(S):
                    X, Y = px * S + sx + 0.5, py * S + sy + 0.5
                    if (X - cx) ** 2 + (Y - cy) ** 2 <= r * r and not inside_glyph(X / S, Y / S):
                        acc += 1
            data.append(round(235 * acc / (S * S)))
    return bytes(data)


def write_preview(alpha, path):
    """PNG de muestra: icono pintado violeta sobre un fondo a cuadros (para ver el calado)."""
    w = h = ICON * 4
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            a = alpha[(y // 4) * ICON + x // 4] / 255
            bg = 200 if ((x // 16) + (y // 16)) % 2 else 120
            fg = (0xB7, 0x65, 0xFF)
            row += bytes(round(fg[c] * a + bg * (1 - a)) for c in range(3))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", raw) + chunk(b"IEND", b"")
    path.write_bytes(png)


# ---------------------------------------------------------------- programa
def build():
    A = Asm(CODE)

    # hook_tap: entra desde 0x0801E578 con r0=y, r1=pendiente(=0), r2=x, r3=pagina, r5=estado
    A.label("hook_tap")
    A.cmp_i(3, 2)
    A.bcond("ne", "tap_orig")
    A.cmp_i(2, TAP_X_MIN)
    A.bcond("lt", "tap_orig")
    A.movw(6, 319)
    A.cmp_r(2, 6)
    A.bcond("gt", "tap_orig")
    A.mov_r(6, 0)
    A.subs_i8(6, 0x23)
    A.cmp_i(6, 0xA9)
    A.bcond("hi", "tap_orig")
    A.sub_sp(8)
    A.movw(0, 0xCD04)
    A.movt(0, 0x0002)
    A.str_sp(0, 0)
    A.movs(0, 0)
    A.str_sp(0, 4)
    A.add_rd_sp(0, 0)
    A.movs(1, 8)
    A.bl(F_SPI_SEND)
    A.add_sp(8)
    A.add_sp(0x6C)
    A.pop([4, 5, 6, 7, "pc"], "pop {r4, r5, r6, r7, pc}")
    A.label("tap_orig")
    A.movw(6, 0x806)
    A.bw(0x0801E57C)

    # hook_overlay: reemplaza "bl lv_obj_remove_flag" en 0x08022C10 (r0 = r4 = capa, r1 = 0x12).
    # Hace la llamada original y cuelga el boton de la capa; vuelve a 0x08022C14 intacto.
    A.label("hook_overlay")
    A.push([4, "lr"], "push {r4, lr}")
    A.bl(F_REMOVE_FLAG)
    A.mov_r(0, 4)
    A.bl("add_button")
    A.pop([4, "pc"], "pop {r4, pc}")

    # media_ctor: constructor original + boton si no hay capa decorativa
    A.label("media_ctor")
    A.push([4, "lr"], "push {r4, lr}")
    A.bl(F_MEDIA_CTOR)
    A.mov_r(4, 0)
    A.movw(0, OVERLAY_PTR & 0xFFFF)
    A.movt(0, OVERLAY_PTR >> 16)
    A.ldr_i(0, 0, 4)
    A.cmp_i(0, 0)
    A.bcond("ne", "mc_done")
    A.bl(F_SCREEN_ACTIVE)
    A.bl("add_button")
    A.label("mc_done")
    A.mov_r(0, 4)
    A.pop([4, "pc"], "pop {r4, pc}")

    # add_button(r0 = padre)
    A.label("add_button")
    A.push([4, "lr"], "push {r4, lr}")
    A.cmp_i(0, 0)
    A.bcond("eq", "ab_done")
    A.bl(F_IMG_CREATE)
    A.mov_r(4, 0)
    A.cmp_i(0, 0)
    A.bcond("eq", "ab_done")
    A.movw_label(1, "icon_dsc", 0)
    A.movw_label(1, "icon_dsc", 1)
    A.bl(F_IMG_SET_SRC)
    A.mov_r(0, 4)
    A.movw(1, BTN_X)
    A.movs(2, BTN_Y)
    A.bl(F_SET_POS)
    A.mov_r(0, 4)
    A.bl(F_TINT)
    A.mov_r(0, 4)
    A.movs(1, 0x12)
    A.bl(F_REMOVE_FLAG)
    A.label("ab_done")
    A.pop([4, "pc"], "pop {r4, pc}")

    code_end = A.pc
    dsc_addr = (code_end + 3) & ~3
    data_addr = dsc_addr + 24
    A.labels["icon_dsc"] = dsc_addr
    code, listing = A.assemble()
    verify(listing)

    icon = make_icon()
    stride = ICON
    dsc = struct.pack("<BBHHHHHIII", 0x19, 0x0E, 0, ICON, ICON, stride, 0, len(icon), data_addr, 0)
    assert len(dsc) == 24
    blob = code + b"\x00" * (dsc_addr - code_end) + dsc + icon
    return A, blob, listing, icon


def check_no_branch_into(mem):
    """Ningun salto/llamada del firmware original puede caer en la mitad de un sitio parcheado,
    y cada sitio tiene que ser exactamente una instruccion de 4 bytes."""
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    blob = bytes(mem[a] for a in range(0x08011000, IMG_END))
    bad = []
    for site in SITES:
        ins = list(md.disasm(blob[site - 0x08011000:site - 0x08011000 + 4], site))
        assert len(ins) == 1 and ins[0].size == 4, f"{site:#x} no es una instruccion de 32 bits"
    mids = {site + 2 for site in SITES}
    # barrido lineal de 2 en 2: cualquier b/bl/cbz/cbnz/b<cond> cuyo destino sea la mitad de un sitio
    for off in range(0, len(blob) - 4, 2):
        for ins in md.disasm(blob[off:off + 4], 0x08011000 + off, count=1):
            if ins.mnemonic.startswith(("b", "cb")) and ins.op_str.startswith("#"):
                try:
                    t = int(ins.op_str[1:], 16)
                except ValueError:
                    continue
                if t in mids:
                    bad.append((ins.address, ins.mnemonic, t))
    if bad:
        sys.exit("saltos hacia la mitad de un sitio parcheado: " + ", ".join(f"{a:#x} {m} -> {t:#x}" for a, m, t in bad))
    print("  verificado: ningun salto cae en la mitad de los sitios parcheados")


def patch_sites(mem, A):
    targets = {
        0x0801E578: A.labels["hook_tap"],
        0x08022C10: A.labels["hook_overlay"],
        0x08021758: A.labels["media_ctor"],
        0x0802229A: A.labels["media_ctor"],
        0x08022BAC: A.labels["media_ctor"],
    }
    check_no_branch_into(mem)
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    for site, (orig, desc, kind) in SITES.items():
        cur = bytes(mem[site + i] for i in range(4))
        if cur.hex() != orig:
            sys.exit(f"bytes inesperados en {site:#x}: {cur.hex()} (esperaba {orig})")
        new = Asm.branch32(site, targets[site], kind == "bl")
        ins = list(md.disasm(new, site))[0]
        assert ins.mnemonic == kind and int(ins.op_str[1:], 16) == targets[site]
        for i in range(4):
            mem[site + i] = new[i]
        print(f"  {site:#010x}: {desc}  ({orig} -> {new.hex()})")


# ---------------------------------------------------------------- hex
def parse_hex(lines):
    base, recs = 0, []
    for idx, s in enumerate(lines):
        s = s.strip()
        if not s.startswith(":"):
            continue
        b = bytes.fromhex(s[1:])
        n, off, typ = b[0], (b[1] << 8) | b[2], b[3]
        if typ == 0:
            recs.append((idx, base + off, b))
        elif typ == 4:
            base = int.from_bytes(b[4:6], "big") << 16
    return recs


def rec(addr16, data):
    b = bytes([len(data), addr16 >> 8, addr16 & 0xFF, 0]) + data
    return ":" + (b + bytes([(-sum(b)) & 0xFF])).hex().upper() + "\r\n"


def main():
    raw = SRC.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SRC_SHA:
        sys.exit("el hex de partida no es el parcheado esperado")
    lines = raw.decode("ascii").splitlines(keepends=True)
    recs = parse_hex(lines)
    mem = {}
    for _, start, b in recs:
        for k, x in enumerate(b[4:4 + b[0]]):
            mem[start + k] = x
    assert max(mem) + 1 == IMG_END

    A, blob, listing, icon = build()
    print(f"codigo nuevo: {CODE:#x}..{CODE + len(blob):#x} ({len(blob)} bytes)")
    for a, b, t in listing:
        print(f"  {a:08X}: {b.hex():8}  {t}")
    patch_sites(mem, A)

    # reescribir registros existentes que cambiaron
    for idx, start, b in recs:
        rb = bytearray(b)
        changed = False
        for k in range(b[0]):
            if mem[start + k] != b[4 + k]:
                rb[4 + k] = mem[start + k]
                changed = True
        if changed:
            rb[-1] = (-sum(rb[:-1])) & 0xFF
            eol = lines[idx][len(lines[idx].rstrip("\r\n")):]
            lines[idx] = ":" + rb.hex().upper() + eol

    # agregar registros nuevos despues del ultimo registro de datos (mismo segmento 0x0811)
    assert (CODE >> 16) == (IMG_END - 1) >> 16 == ((CODE + len(blob)) >> 16)
    last_idx = recs[-1][0]
    new_lines = [rec((CODE + i) & 0xFFFF, blob[i:i + 16]) for i in range(0, len(blob), 16)]
    lines[last_idx + 1:last_idx + 1] = new_lines

    out = "".join(lines).encode("ascii")
    DST.parent.mkdir(exist_ok=True)
    DST.write_bytes(out)
    write_preview(icon, PREVIEW)

    # verificacion: releer, comparar con lo esperado
    m2 = {}
    for _, start, b in parse_hex(out.decode("ascii").splitlines()):
        for k, x in enumerate(b[4:4 + b[0]]):
            m2[start + k] = x
    for line in out.decode("ascii").splitlines():
        assert sum(bytes.fromhex(line[1:])) & 0xFF == 0
    diff_old = sorted(a for a in mem if a < IMG_END and m2[a] != {**{s: 0 for s in []}}.get(a, None) and False)
    changed_old = [a for a in range(0x08011000, IMG_END) if m2[a] != mem[a]]
    assert not changed_old, "el hex releido no coincide con la memoria parcheada"
    for i, x in enumerate(blob):
        assert m2[CODE + i] == x
    print("escrito:", DST)
    print("SHA256:", hashlib.sha256(out).hexdigest())
    print("preview:", PREVIEW)


if __name__ == "__main__":
    main()
