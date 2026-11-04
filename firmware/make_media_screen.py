"""Genera el firmware MH2435 con una pantalla nueva "MEDIA" (Default Screen = 4) con boton Play/Pause.

La pantalla Media (galeria) NO se toca. Parte del hex con el doble toque parcheado (04 CD) y agrega:

  * Comando Default Screen (LCD grupo 5, cmd 0x0A): acepta el valor 4 y lo guarda como indice 3.
  * Setter del indice (0x0801A1D4): acepta 3 (antes solo 0..2).
  * Dispatcher (0x08022B5C): al cambiar Default Screen reconstruye la pagina 1 aunque ya este visible.
  * p1_ctor: envoltorio del constructor de Mouse Stats (pagina 1). Si el indice es 3, cuelga encima
    un panel opaco: fondo del menu de fabrica + icono play/pause de 72x72 + titulo "MEDIA".
    Internamente sigue siendo la pagina 1: deslizar abre el menu, el doble toque sigue andando.
  * hook_tap: en la pagina 1 con indice 3, un toque en la franja central (x 100..220) manda
    [04 CD 02 00 00 00 00 00] al nRF; la PC (dongle_media.py) lo convierte en Play/Pause.

Etapa 1 (por defecto): el indice 3 NO se guarda en flash (la funcion de guardado lo rechaza), asi
un reinicio vuelve a la pantalla anterior. Etapa 2 (--persist): tambien parchea el guardado.

Solo escribe archivos. No habla con el dongle.
"""
import argparse
import hashlib
import pathlib
import struct
import sys

import capstone

import make_media_button as base
from make_media_button import Asm, imm_txt, parse_hex, rec, verify

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = base.SRC  # hex con el doble toque parcheado
SRC_SHA = base.SRC_SHA
IMG_END = base.IMG_END
CODE = 0x08119020

F_SPI_SEND = 0x08015A68
F_P1_CTOR = 0x0801F20C        # constructor original de Mouse Stats (pagina 1)
F_IDX_GET = 0x08019F78        # indice Default Screen (& 3)
F_SCREEN_ACTIVE = 0x0806B474  # lv_screen_active()
F_IMG_CREATE = 0x0805CC48
F_IMG_SET_SRC = 0x0805D748
F_SET_POS = 0x08068CA0
F_TINT = 0x0801B68C
F_REMOVE_FLAG = 0x08067F98
F_LABEL = 0x0801EB48          # helper: (padre, texto, fuente, color, x, y, w, h, align)
MENU_BG_DSC = 0x0808D814      # fondo RGB565 320x170 que usan las paginas del menu
FONT = 0x080A3758             # fuente que usa la pantalla de arranque ("WLMOUSE")
TEXT_COLOR = 0x00F1F0EC
CFG_BYTE1 = 0x200014FD        # bits 0-1 = indice Default Screen

ICON = 72
ICON_X, ICON_Y = (320 - ICON) // 2, 28
TITLE_Y, TITLE_H = 108, 30
TAP_X0, TAP_X1 = 100, 220

# Instrucciones de 16 bits donde solo cambia el inmediato (mismo tamano, mismo tipo)
IMM_EDITS = {
    0x08024A46: ("0428", "0528", "cmp r0, #4", "cmp r0, #5", "cmd 0x0A: aceptar valor 4"),
    0x0801A1D4: ("0228", "0328", "cmp r0, #2", "cmp r0, #3", "setter: aceptar indice 3"),
    0x08022BB0: ("0129", "ff29", "cmp r1, #1", "cmp r1, #0xff", "dispatcher: reconstruir pagina 1 siempre"),
}
PERSIST_EDIT = {
    0x08019D42: ("0328", "0428", "cmp r0, #3", "cmp r0, #4", "guardado: permitir indice 3"),
}
# Instrucciones de 32 bits que pasan a ser saltos/llamadas a codigo nuevo
SITES = {
    0x0801E578: ("40f60606", "b.w", "hook_tap", "movw r6, #0x806 -> b.w hook_tap"),
    0x08024A5A: ("22fa01f1", "b.w", "hook_map", "lsr.w r1, r2, r1 -> b.w hook_map"),
    0x08021768: (None, "b.w", "p1_ctor", "b.w 0x801f20c -> b.w p1_ctor"),
    0x08022BBA: (None, "b.w", "p1_ctor", "b.w 0x801f20c -> b.w p1_ctor"),
}


class Asm2(Asm):
    def ldrb_i(self, rt, rn, imm):
        self.h(0x7800 | imm << 6 | rn << 3 | rt, f"ldrb r{rt}, [r{rn}]" if imm == 0 else f"ldrb r{rt}, [r{rn}, {imm_txt(imm)}]")

    def and_imm8(self, rd, rn, imm):  # AND.W T1 con inmediato de 8 bits sin rotar
        assert 0 <= imm <= 255
        self._emit(4, lambda a: struct.pack("<HH", 0xF000 | rn, rd << 8 | imm), f"and r{rd}, r{rn}, {imm_txt(imm)}")

    def raw(self, hexbytes, text):
        b = bytes.fromhex(hexbytes)
        self._emit(len(b), lambda a: b, text)

    def adr_label(self, rd, label):  # cargar direccion absoluta con movw/movt
        self.movw_label(rd, label, 0)
        self.movw_label(rd, label, 1)


def build():
    A = Asm2(CODE)

    # ---- hook_tap: r0=y r1=0 r2=x r3=pagina r5=estado r7=1<<pagina (r6 libre: lo pisa el original)
    A.label("hook_tap")
    A.cmp_i(3, 1)
    A.bcond("ne", "tap_orig")
    A.movw(6, CFG_BYTE1 & 0xFFFF)
    A.movt(6, CFG_BYTE1 >> 16)
    A.ldrb_i(6, 6, 0)
    A.and_imm8(6, 6, 3)
    A.cmp_i(6, 3)
    A.bcond("ne", "tap_orig")
    A.cmp_i(2, TAP_X0)
    A.bcond("lt", "tap_orig")
    A.cmp_i(2, TAP_X1)
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

    # ---- hook_map: cmd 0x0A, r0 = valor (0 ya convertido a 3), r1 = valor*8, r2 = tabla 0x02010002
    A.label("hook_map")
    A.cmp_i(0, 4)
    A.bcond("ne", "hm_orig")
    A.movs(1, 3)
    A.bw(0x08024A5E)
    A.label("hm_orig")
    A.raw("22fa01f1", "lsr.w r1, r2, r1")
    A.bw(0x08024A5E)

    # ---- p1_ctor: Mouse Stats original + panel MEDIA si el indice es 3
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

    # ---- add_panel(r0 = pantalla)
    A.label("add_panel")
    A.push([4, 5, 6, "lr"], "push {r4, r5, r6, lr}")
    A.sub_sp(0x18)
    A.cmp_i(0, 0)
    A.bcond("eq", "ap_done")
    A.mov_r(5, 0)
    # fondo opaco (el mismo del menu)
    A.bl(F_IMG_CREATE)
    A.mov_r(4, 0)
    A.cmp_i(0, 0)
    A.bcond("eq", "ap_done")
    A.movw(1, MENU_BG_DSC & 0xFFFF)
    A.movt(1, MENU_BG_DSC >> 16)
    A.bl(F_IMG_SET_SRC)
    A.mov_r(0, 4)
    A.movs(1, 0)
    A.movs(2, 0)
    A.bl(F_SET_POS)
    A.mov_r(0, 4)
    A.movs(1, 0x12)
    A.bl(F_REMOVE_FLAG)
    # icono play/pause
    A.mov_r(0, 5)
    A.bl(F_IMG_CREATE)
    A.mov_r(4, 0)
    A.cmp_i(0, 0)
    A.bcond("eq", "ap_done")
    A.adr_label(1, "icon_dsc")
    A.bl(F_IMG_SET_SRC)
    A.mov_r(0, 4)
    A.movs(1, ICON_X)
    A.movs(2, ICON_Y)
    A.bl(F_SET_POS)
    A.mov_r(0, 4)
    A.bl(F_TINT)
    A.mov_r(0, 4)
    A.movs(1, 0x12)
    A.bl(F_REMOVE_FLAG)
    # titulo
    A.movs(0, 0)
    A.str_sp(0, 0)            # x
    A.movs(0, TITLE_Y)
    A.str_sp(0, 4)            # y
    A.movw(0, 320)
    A.str_sp(0, 8)            # w
    A.movs(0, TITLE_H)
    A.str_sp(0, 12)           # h
    A.movs(0, 2)
    A.str_sp(0, 16)           # align centro
    A.mov_r(0, 5)
    A.adr_label(1, "title_txt")
    A.movw(2, FONT & 0xFFFF)
    A.movt(2, FONT >> 16)
    A.movw(3, TEXT_COLOR & 0xFFFF)
    A.movt(3, TEXT_COLOR >> 16)
    A.bl(F_LABEL)
    A.label("ap_done")
    A.add_sp(0x18)
    A.pop([4, 5, 6, "pc"], "pop {r4, r5, r6, pc}")

    code_end = A.pc
    title_addr = (code_end + 3) & ~3
    title = b"MEDIA\x00\x00\x00"
    dsc_addr = title_addr + len(title)
    data_addr = dsc_addr + 24
    A.labels["title_txt"] = title_addr
    A.labels["icon_dsc"] = dsc_addr
    code, listing = A.assemble()
    verify(listing)

    base.ICON = ICON
    icon = make_icon(ICON)
    dsc = struct.pack("<BBHHHHHIII", 0x19, 0x0E, 0, ICON, ICON, ICON, 0, len(icon), data_addr, 0)
    blob = code + b"\x00" * (title_addr - code_end) + title + dsc + icon
    return A, blob, listing, icon


def make_icon(size):
    """Mismo diseno que el boton chico, escalado: disco solido con play/pause calado."""
    S = 4
    k = size / 48.0
    n = size * S
    cx = cy = n / 2
    r = 22 * k * S

    def glyph(x, y):  # coordenadas del diseno de 48x48
        if 11 <= x <= 23 and 14 <= y <= 34:
            t = (x - 11) / 12.0
            if abs(y - 24) <= 10 * (1 - t):
                return True
        return (26.5 <= x <= 30 or 33.5 <= x <= 37) and 14 <= y <= 34

    out = bytearray()
    for py in range(size):
        for px in range(size):
            acc = 0
            for sy in range(S):
                for sx in range(S):
                    X, Y = px * S + sx + 0.5, py * S + sy + 0.5
                    if (X - cx) ** 2 + (Y - cy) ** 2 <= r * r and not glyph(X / S / k, Y / S / k):
                        acc += 1
            out.append(round(235 * acc / (S * S)))
    return bytes(out)


def check_sites(mem):
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    blob = bytes(mem[a] for a in range(0x08011000, IMG_END))
    sites32 = set(SITES)
    for s in sites32:
        ins = list(md.disasm(blob[s - 0x08011000:s - 0x08011000 + 4], s))
        assert len(ins) == 1 and ins[0].size == 4, f"{s:#x} no es una instruccion de 32 bits"
    mids = {s + 2 for s in sites32}
    bad = []
    for off in range(0, len(blob) - 4, 2):
        for ins in md.disasm(blob[off:off + 4], 0x08011000 + off, count=1):
            if ins.mnemonic.startswith(("b", "cb")) and ins.op_str.startswith("#"):
                try:
                    t = int(ins.op_str[1:], 16)
                except ValueError:
                    continue
                if t in mids:
                    bad.append(f"{ins.address:#x} {ins.mnemonic} -> {t:#x}")
    if bad:
        sys.exit("saltos hacia la mitad de un sitio parcheado: " + ", ".join(bad))
    print("  verificado: ningun salto cae en la mitad de los sitios de 32 bits")


def apply(mem, A, persist):
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    check_sites(mem)
    for site, (orig, kind, target, desc) in SITES.items():
        cur = bytes(mem[site + i] for i in range(4))
        ins = list(md.disasm(cur, site))[0]
        if orig is None:  # tiene que ser b.w 0x801f20c
            assert ins.mnemonic == "b.w" and int(ins.op_str[1:], 16) == F_P1_CTOR, (hex(site), ins.op_str)
        elif cur.hex() != orig:
            sys.exit(f"bytes inesperados en {site:#x}: {cur.hex()} (esperaba {orig})")
        new = Asm.branch32(site, A.labels[target], kind == "bl")
        chk = list(md.disasm(new, site))[0]
        assert chk.mnemonic == kind and int(chk.op_str[1:], 16) == A.labels[target]
        for i in range(4):
            mem[site + i] = new[i]
        print(f"  {site:#010x}: {desc}  ({cur.hex()} -> {new.hex()})")
    edits = dict(IMM_EDITS)
    if persist:
        edits.update(PERSIST_EDIT)
    for site, (old, new, old_txt, new_txt, desc) in edits.items():
        cur = bytes(mem[site + i] for i in range(2))
        if cur.hex() != old:
            sys.exit(f"bytes inesperados en {site:#x}: {cur.hex()} (esperaba {old})")
        got_old = list(md.disasm(cur, site))[0]
        got_new = list(md.disasm(bytes.fromhex(new), site))[0]
        assert f"{got_old.mnemonic} {got_old.op_str}" == old_txt, got_old.op_str
        assert f"{got_new.mnemonic} {got_new.op_str}" == new_txt, got_new.op_str
        nb = bytes.fromhex(new)
        mem[site], mem[site + 1] = nb[0], nb[1]
        print(f"  {site:#010x}: {desc}  ({old_txt} -> {new_txt})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persist", action="store_true", help="etapa 2: guardar el indice 3 en flash")
    args = ap.parse_args()
    dst = ROOT / ("patched-screen-persist" if args.persist else "patched-screen") / SRC.name

    raw = SRC.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SRC_SHA:
        sys.exit("el hex de partida no es el parcheado esperado")
    lines = raw.decode("ascii").splitlines(keepends=True)
    recs = parse_hex(lines)
    mem = {}
    for _, start, b in recs:
        for k, x in enumerate(b[4:4 + b[0]]):
            mem[start + k] = x
    orig_mem = dict(mem)
    assert max(mem) + 1 == IMG_END

    A, blob, listing, icon = build()
    print(f"codigo nuevo: {CODE:#x}..{CODE + len(blob):#x} ({len(blob)} bytes)")
    for a, b, t in listing:
        print(f"  {a:08X}: {b.hex():8}  {t}")
    apply(mem, A, args.persist)

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

    assert (CODE >> 16) == ((IMG_END - 1) >> 16) == ((CODE + len(blob)) >> 16)
    last_idx = recs[-1][0]
    lines[last_idx + 1:last_idx + 1] = [rec((CODE + i) & 0xFFFF, blob[i:i + 16]) for i in range(0, len(blob), 16)]
    out = "".join(lines).encode("ascii")
    dst.parent.mkdir(exist_ok=True)
    dst.write_bytes(out)
    base.write_preview(icon, dst.parent / "icono_preview.png") if ICON == 48 else write_preview(icon, dst.parent / "icono_preview.png")

    # verificacion: releer y comparar
    m2 = {}
    for _, start, b in parse_hex(out.decode("ascii").splitlines()):
        for k, x in enumerate(b[4:4 + b[0]]):
            m2[start + k] = x
    for line in out.decode("ascii").splitlines():
        assert sum(bytes.fromhex(line[1:])) & 0xFF == 0
    assert all(m2[a] == mem[a] for a in mem)
    assert all(m2[CODE + i] == x for i, x in enumerate(blob))
    changed = [a for a in orig_mem if orig_mem[a] != m2[a]]
    print(f"bytes cambiados dentro de la imagen original: {len(changed)}")
    print("escrito:", dst)
    print("SHA256:", hashlib.sha256(out).hexdigest())


def write_preview(alpha, path):
    import zlib
    sz = int(len(alpha) ** 0.5)
    sc = 3
    w = h = sz * sc
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            a = alpha[(y // sc) * sz + x // sc] / 255
            bg = 200 if ((x // 16) + (y // 16)) % 2 else 120
            row += bytes(round(c * a + bg * (1 - a)) for c in (0xB7, 0x65, 0xFF))
        rows.append(bytes(row))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


if __name__ == "__main__":
    main()
