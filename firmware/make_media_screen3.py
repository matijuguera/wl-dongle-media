"""v3 de "Media Control" (Default Screen = 4).

Todo lo de v2 (panel en (0,35), bateria, 5 botones, play/pause que alterna) mas:

  * Comando PC -> dongle (LCD grupo 5, cmd 0x20): payload [6]=estado (0 nada, 1 sonando, 2 pausado),
    [7..]=nombre de la salida de audio (ASCII, termina en 0). El handler corre en la tarea de transporte:
    solo copia al buzon en RAM y responde A1. Hook: el "comando desconocido" en 0x080246B4.
  * Buzon en RAM libre (0x2011D400; .bss termina en 0x2011D148, la pila arranca en 0x20140000 y nada
    del firmware referencia ese hueco): magic, seq, applied, estado, guard, texto[40].
  * my_tick: engancha la llamada 0x080175B8 del loop principal (tarea LVGL). Si el buzon cambio, pone
    el nombre en el label del panel y, si el estado es 1/2, el icono de play/pause. Guard: si un intento
    anterior quedo a medias (reinicio), desactiva el buzon en vez de reintentar.
  * Tocar la franja del nombre (abajo) manda 0x15 -> la PC pasa a la siguiente salida de audio.
  * Boton chico arriba a la izquierda en Media Control (-> Media) y en Media (-> Media Control):
    cambia el Default Screen (setter 0x0801A1D4) y arma la otra pagina con lv_async_call, igual que el
    firmware al volver del menu.

Solo escribe archivos. No habla con el dongle.
"""
import argparse
import hashlib
import pathlib
import struct
import sys

import capstone

import make_media_button as base
import make_media_screen as v1
import make_media_screen2 as v2
from make_media_button import Asm, parse_hex, rec, verify
from make_media_screen2 import Asm3, imm_txt

ROOT = pathlib.Path(__file__).resolve().parent.parent
CODE = 0x08119020
IMG_END = base.IMG_END

# firmware
F_SPI_SEND = 0x08015A68
F_P1_CTOR = 0x0801F20C
F_MEDIA_CTOR = 0x0801F100
F_IDX_GET = 0x08019F78
F_IDX_SET = 0x0801A1D4
F_SCREEN_ACTIVE = 0x0806B474
F_OBJ_CREATE = 0x080640A4
F_REMOVE_STYLE_ALL = 0x080683B8
F_SET_SIZE = 0x08068D80
F_IMG_CREATE = 0x0805CC48
F_IMG_SET_SRC = 0x0805D748
F_SET_POS = 0x08068CA0
F_TINT = 0x0801B68C
F_REMOVE_FLAG = 0x08067F98
F_LABEL = 0x0801EB48
F_LABEL_SET_TEXT = 0x08060DE0
F_BATT_WIDGET = 0x0801B7A8
F_BATT_WIDGET2 = 0x0801BAC8
F_STATUS_UPDATE = 0x08022FEC
F_ASYNC_CALL = 0x0803A868
F_LOOP_ORIG = 0x0802255C       # llamada original en 0x080175B8
THUNK_MEDIA = 0x0802174D       # async: limpia STATE[4] y arma la pagina 2 (Media)
THUNK_P1 = 0x0802175D          # async: limpia STATE[4] y arma la pagina 1 (-> p1_ctor)
CMD_UNKNOWN = 0x08024AA4       # respuesta "comando desconocido"
CMD_OK = 0x08024B60            # respuesta A1
MENU_BG_DSC = 0x0808D814
FONT_TITLE = 0x080A3758        # 22 px
FONT_NAME = 0x080A3710         # 18 px (ASCII completo)
TEXT_COLOR = 0x00F1F0EC
CFG_BYTE1 = 0x200014FD
STATE = 0x2000163C
RAW_DEFAULT = 0x200015FB       # valor crudo que devuelve el get 0x8A (1..4)

MBOX = 0x2011D400
MAGIC = 0x3144454D             # "MED1"
MB_SEQ, MB_APPLIED, MB_STATE, MB_GUARD, MB_TEXT = 4, 8, 12, 13, 16
TEXT_MAX = 39

PAGE_Y = 35
BTN_TOP, NAME_TOP = 40, 126    # en coordenadas del panel
CODES = [0x14, 0x12, 0x10, 0x11, 0x13]
CODE_OUTPUT = 0x15
BUTTONS = [("vol_down", 44, 32), ("prev", 48, 96), ("play", 58, 160), ("next", 48, 224), ("vol_up", 44, 288)]
ROW_CY = 86
SWITCH_SIZE, SWITCH_X, SWITCH_Y = 28, 8, 6
DEVNAME_IDX = 4                # hijos del panel: bg, bateria, bateria2, titulo, nombre, switch, botones...

SITES = {
    0x0801E578: ("40f60606", "b.w", "hook_tap"),
    0x08024A5A: ("22fa01f1", "b.w", "hook_map"),
    0x08021768: ("fdf750bd", "b.w", "p1_ctor"),
    0x08022BBA: ("fcf727bb", "b.w", "p1_ctor"),
    0x08021758: ("fdf7d2bc", "b.w", "media_ctor"),
    0x0802229A: ("fcf731bf", "b.w", "media_ctor"),
    0x08022BAC: ("fcf7a8ba", "b.w", "media_ctor"),
    0x080246B4: (None, "bhi.w", "hook_cmd"),
    0x080175B8: (None, "bl", "my_tick"),
}
SITE_ORIG_TARGET = {0x080246B4: ("bhi.w", CMD_UNKNOWN), 0x080175B8: ("bl", F_LOOP_ORIG)}


class Asm4(Asm3):
    def ldr_i(self, rt, rn, imm):
        self.h(0x6800 | (imm // 4) << 6 | rn << 3 | rt, f"ldr r{rt}, [r{rn}]" if imm == 0 else f"ldr r{rt}, [r{rn}, {imm_txt(imm)}]")

    def str_i(self, rt, rn, imm):
        self.h(0x6000 | (imm // 4) << 6 | rn << 3 | rt, f"str r{rt}, [r{rn}]" if imm == 0 else f"str r{rt}, [r{rn}, {imm_txt(imm)}]")

    def strb_i(self, rt, rn, imm):
        self.h(0x7000 | imm << 6 | rn << 3 | rt, f"strb r{rt}, [r{rn}]" if imm == 0 else f"strb r{rt}, [r{rn}, {imm_txt(imm)}]")

    def adds_imm8(self, rdn, imm):
        self.h(0x3000 | rdn << 8 | imm, f"adds r{rdn}, {imm_txt(imm)}")

    def adds_rrr(self, rd, rn, rm):
        self.h(0x1800 | rm << 6 | rn << 3 | rd, f"adds r{rd}, r{rn}, r{rm}")

    def adds_rri3(self, rd, rn, imm):
        self.h(0x1C00 | imm << 6 | rn << 3 | rd, f"adds r{rd}, r{rn}, {imm_txt(imm)}")

    @staticmethod
    def bcond32(a, target, cond):
        off = target - (a + 4)
        assert -(1 << 20) <= off < (1 << 20) and off % 2 == 0, hex(off)
        s = 1 if off < 0 else 0
        o = off & ((1 << 21) - 1)
        h1 = 0xF000 | s << 10 | Asm3.COND[cond] << 6 | ((o >> 12) & 0x3F)
        h2 = 0x8000 | ((o >> 18) & 1) << 13 | ((o >> 19) & 1) << 11 | ((o >> 1) & 0x7FF)
        return struct.pack("<HH", h1, h2)

    def label_call(self, parent_reg, text_label, font, x, y, w, h):
        self.movs(0, x) if x < 256 else self.movw(0, x)
        self.str_sp(0, 0)
        self.movs(0, y)
        self.str_sp(0, 4)
        self.movw(0, w)
        self.str_sp(0, 8)
        self.movs(0, h)
        self.str_sp(0, 12)
        self.movs(0, 2)
        self.str_sp(0, 16)
        self.mov_r(0, parent_reg)
        self.addr(1, text_label)
        self.mov32(2, font)
        self.mov32(3, TEXT_COLOR)
        self.bl(F_LABEL)


# ------------------------------------------------------------------ iconos extra (28 px)
def glyph_extra(name, u, v):
    if name == "to_gallery":  # marco de foto con montana y sol (sin disco)
        frame = (0.12 <= u <= 0.88 and 0.18 <= v <= 0.82) and not (0.20 <= u <= 0.80 and 0.26 <= v <= 0.74)
        mount = v2._tri((u, v), (0.20, 0.74), (0.45, 0.42), (0.66, 0.74)) or \
            v2._tri((u, v), (0.48, 0.74), (0.64, 0.52), (0.80, 0.74))
        sun = (u - 0.66) ** 2 + (v - 0.38) ** 2 <= 0.07 ** 2
        return frame or mount or sun
    raise ValueError(name)


def make_icon_plain(name, size):
    S = 4
    out = bytearray()
    for py in range(size):
        for px in range(size):
            acc = 0
            for sy in range(S):
                for sx in range(S):
                    u = (px + (sx + 0.5) / S) / size
                    v = (py + (sy + 0.5) / S) / size
                    if glyph_extra(name, u, v):
                        acc += 1
            out.append(round(255 * acc / (S * S)))
    return bytes(out)


# ------------------------------------------------------------------ codigo
def build():
    A = Asm4(CODE)

    # ---- hook_cmd (tarea de transporte): r1 = cmd, r8 = buffer del pedido. Primero: rango de bhi.w.
    A.label("hook_cmd")
    A.cmp_i(1, 0x20)
    A.bcond("eq", "hc_go")
    A.bw(CMD_UNKNOWN)
    A.label("hc_go")
    A.mov32(2, MBOX)
    A.movs(5, 0)                         # texto[39] = 0 siempre (la RAM arranca con basura)
    A.mov_r(6, 2)
    A.adds_imm8(6, TEXT_MAX)
    A.strb_i(5, 6, MB_TEXT)
    A.mov_r(3, 8)
    A.ldrb_i(0, 3, 6)
    A.strb_i(0, 2, MB_STATE)
    A.adds_imm8(3, 7)
    A.movs(4, 0)
    A.label("hc_loop")
    A.ldrb_r(5, 3, 4)
    A.adds_rrr(6, 2, 4)
    A.strb_i(5, 6, MB_TEXT)
    A.cmp_i(5, 0)
    A.bcond("eq", "hc_end")
    A.adds_imm8(4, 1)
    A.cmp_i(4, TEXT_MAX)
    A.bcond("lo", "hc_loop")
    A.movs(5, 0)
    A.adds_rrr(6, 2, 4)
    A.strb_i(5, 6, MB_TEXT)
    A.label("hc_end")
    A.ldr_i(0, 2, MB_SEQ)
    A.adds_imm8(0, 1)
    A.str_i(0, 2, MB_SEQ)
    A.mov32(0, MAGIC)
    A.str_i(0, 2, 0)
    A.bw(CMD_OK)

    # ---- hook_tap: r0=y r1=0 r2=x r3=pagina r5=estado r7=1<<pagina; r6 libre
    A.label("hook_tap")
    A.cmp_i(3, 2)
    A.bcond("eq", "ht_media")
    A.cmp_i(3, 1)
    A.bcond("ne", "tap_orig")
    A.mov32(6, CFG_BYTE1)
    A.ldrb_i(6, 6, 0)
    A.and_imm8(6, 6, 3)
    A.cmp_i(6, 3)
    A.bcond("ne", "tap_orig")
    A.mov_r(6, 0)
    A.subs_imm8(6, PAGE_Y)
    A.cmp_i(6, 169)
    A.bcond("hi", "tap_orig")
    A.cmp_i(6, BTN_TOP)
    A.bcond("hs", "ht_lower")
    A.cmp_i(2, 56)                       # franja de arriba: solo el icono chico
    A.bcond("hs", "tap_orig")
    A.movs(0, 2)
    A.bl("switch_to")
    A.b("ht_ret")
    A.label("ht_lower")
    A.movw(1, 319)
    A.cmp_r(2, 1)
    A.bcond("hi", "tap_orig")
    A.cmp_i(6, NAME_TOP)
    A.bcond("hs", "ht_name")
    A.lsrs_i(6, 2, 6)
    A.addr(1, "codes")
    A.ldrb_r(1, 1, 6)
    A.bl("send_code")
    A.cmp_i(6, 2)
    A.bcond("ne", "ht_ret")
    A.bl("toggle_pp")
    A.b("ht_ret")
    A.label("ht_name")
    A.movs(1, CODE_OUTPUT)
    A.bl("send_code")
    A.b("ht_ret")
    A.label("ht_media")
    A.mov_r(6, 0)
    A.subs_imm8(6, PAGE_Y)
    A.cmp_i(6, BTN_TOP)
    A.bcond("hs", "tap_orig")
    A.cmp_i(2, 56)
    A.bcond("hs", "tap_orig")
    A.movs(0, 3)
    A.bl("switch_to")
    A.label("ht_ret")
    A.add_sp(0x6C)
    A.pop([4, 5, 6, 7, "pc"], "pop {r4, r5, r6, r7, pc}")
    A.label("tap_orig")
    A.movw(6, 0x806)
    A.bw(0x0801E57C)

    # ---- send_code(r1 = codigo): [04 CD codigo 00 00 00 00 00] al nRF
    A.label("send_code")
    A.push([4, "lr"], "push {r4, lr}")
    A.sub_sp(8)
    A.movw(0, 0xCD04)
    A.str_sp(0, 0)
    A.movs(0, 0)
    A.str_sp(0, 4)
    A.add_rd_sp(0, 0)
    A.strb_i(1, 0, 2)
    A.movs(1, 8)
    A.bl(F_SPI_SEND)
    A.add_sp(8)
    A.pop([4, "pc"], "pop {r4, pc}")

    # ---- switch_to(r0 = indice 2 Media / 3 Media Control)
    A.label("switch_to")
    A.push([4, 5, 6, "lr"], "push {r4, r5, r6, lr}")
    A.mov_r(4, 0)
    A.bl(F_IDX_SET)
    A.mov32(5, STATE)
    A.strb_i(4, 5, 0xD)
    A.mov32(0, RAW_DEFAULT)
    A.adds_rri3(1, 4, 1)
    A.strb_i(1, 0, 0)
    A.movs(0, 0)
    A.strb_i(0, 5, 2)
    A.movs(0, 1)
    A.strb_i(0, 5, 4)
    A.cmp_i(4, 2)
    A.bcond("ne", "sw_ctl")
    A.mov32(0, THUNK_MEDIA)
    A.b("sw_go")
    A.label("sw_ctl")
    A.mov32(0, THUNK_P1)
    A.label("sw_go")
    A.movs(1, 0)
    A.bl(F_ASYNC_CALL)
    A.cmp_i(0, 1)
    A.bcond("eq", "sw_done")
    A.movs(0, 0)
    A.strb_i(0, 5, 4)
    A.label("sw_done")
    A.pop([4, 5, 6, "pc"], "pop {r4, r5, r6, pc}")

    # ---- hook_map: cmd 0x0A, valor 4 -> indice 3
    A.label("hook_map")
    A.cmp_i(0, 4)
    A.bcond("ne", "hm_orig")
    A.movs(1, 3)
    A.bw(0x08024A5E)
    A.label("hm_orig")
    A.raw("22fa01f1", "lsr.w r1, r2, r1")
    A.bw(0x08024A5E)

    # ---- p1_ctor: Mouse Stats + panel si el indice es 3
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

    # ---- media_ctor: galeria original + icono chico (-> Media Control) colgado de la pantalla
    A.label("media_ctor")
    A.push([4, "lr"], "push {r4, lr}")
    A.bl(F_MEDIA_CTOR)
    A.mov_r(4, 0)
    A.bl(F_SCREEN_ACTIVE)
    A.cmp_i(0, 0)
    A.bcond("eq", "mc_done")
    A.addr(1, "dsc_to_ctl")
    A.movs(2, SWITCH_X)
    A.movs(3, PAGE_Y + SWITCH_Y)
    A.bl("put_icon")
    A.label("mc_done")
    A.mov_r(0, 4)
    A.pop([4, "pc"], "pop {r4, pc}")

    # ---- add_panel(r0 = pantalla)
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
    A.mov_r(0, 5)                        # hijo 0: fondo
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
    A.mov_r(0, 5)                        # hijos 1 y 2: bateria
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
    A.label_call(5, "title_txt", FONT_TITLE, 0, 6, 320, 26)        # hijo 3
    A.label_call(5, "empty_txt", FONT_NAME, 0, 132, 320, 24)       # hijo 4: nombre de la salida
    A.mov_r(0, 5)                                                    # hijo 5: icono -> Media
    A.addr(1, "dsc_to_gallery")
    A.movs(2, SWITCH_X)
    A.movs(3, SWITCH_Y)
    A.bl("put_icon")
    for name, size, cx in BUTTONS:                                   # hijos 6..10
        A.mov_r(0, 5)
        A.addr(1, f"dsc_{name}")
        A.movw(2, cx - size // 2)
        A.movs(3, ROW_CY - size // 2)
        A.bl("put_icon")
    A.mov32(0, MBOX)                     # forzar que my_tick reaplique nombre/estado
    A.ldr_i(1, 0, MB_SEQ)
    A.subs_imm8(1, 1)
    A.str_i(1, 0, MB_APPLIED)
    A.bl(F_STATUS_UPDATE)
    A.label("ap_done")
    A.add_sp(0x18)
    A.pop([4, 5, 6, "pc"], "pop {r4, r5, r6, pc}")

    # ---- put_icon(r0=padre, r1=dsc, r2=x, r3=y)
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

    # ---- find_panel() -> r0 = panel (o 0), r1 = imagen play/pausa
    A.label("find_panel")
    A.push([4, 5, 6, 7, "lr"], "push {r4, r5, r6, r7, lr}")
    A.sub_sp(4)
    A.addr(5, "dsc_play")
    A.addr(6, "dsc_pause")
    A.bl(F_SCREEN_ACTIVE)
    A.cmp_i(0, 0)
    A.bcond("eq", "fp_none")
    A.ldr_i(1, 0, 8)
    A.cmp_i(1, 0)
    A.bcond("eq", "fp_none")
    A.ldrh_i(2, 1, 0x28)
    A.ldr_i0(1, 1)
    A.label("fp_outer")
    A.subs_imm8(2, 1)
    A.bcond("mi", "fp_none")
    A.lsls_i(3, 2, 2)
    A.add_r(3, 1)
    A.ldr_i0(3, 3)                       # hijo c de la pantalla
    A.ldr_i(0, 3, 8)
    A.cmp_i(0, 0)
    A.bcond("eq", "fp_outer")
    A.ldrh_i(4, 0, 0x28)
    A.ldr_i0(0, 0)                       # hijos de c
    A.label("fp_inner")
    A.subs_imm8(4, 1)
    A.bcond("mi", "fp_outer")
    A.lsls_i(7, 4, 2)
    A.add_r(7, 0)
    A.ldr_i0(7, 7)
    A.str_sp(3, 0)                       # guardar c
    A.mov_r(3, 7)                        # candidato
    A.ldr_i(7, 7, 0x2C)                  # src
    A.cmp_r(7, 5)
    A.bcond("eq", "fp_found")
    A.cmp_r(7, 6)
    A.bcond("eq", "fp_found")
    A.raw("009b", "ldr r3, [sp]")        # recuperar c
    A.b("fp_inner")
    A.label("fp_found")
    A.mov_r(1, 3)
    A.raw("0098", "ldr r0, [sp]")
    A.b("fp_ret")
    A.label("fp_none")
    A.movs(0, 0)
    A.movs(1, 0)
    A.label("fp_ret")
    A.add_sp(4)
    A.pop([4, 5, 6, 7, "pc"], "pop {r4, r5, r6, r7, pc}")

    # ---- toggle_pp
    A.label("toggle_pp")
    A.push([4, "lr"], "push {r4, lr}")
    A.bl("find_panel")
    A.cmp_i(0, 0)
    A.bcond("eq", "tp_done")
    A.mov_r(4, 1)
    A.ldr_i(0, 4, 0x2C)
    A.addr(1, "dsc_play")
    A.cmp_r(0, 1)
    A.bcond("ne", "tp_set")
    A.addr(1, "dsc_pause")
    A.label("tp_set")
    A.mov_r(0, 4)
    A.bl(F_IMG_SET_SRC)
    A.label("tp_done")
    A.pop([4, "pc"], "pop {r4, pc}")

    # ---- my_tick (tarea LVGL): llamada original + aplicar buzon
    A.label("my_tick")
    A.push([4, 5, 6, 7, "lr"], "push {r4, r5, r6, r7, lr}")
    A.sub_sp(4)
    A.bl(F_LOOP_ORIG)
    A.mov32(4, MBOX)
    A.ldr_i(0, 4, 0)
    A.mov32(1, MAGIC)
    A.cmp_r(0, 1)
    A.bcond("ne", "mt_done")
    A.ldrb_i(0, 4, MB_GUARD)
    A.cmp_i(0, 0)
    A.bcond("eq", "mt_go")
    A.movs(0, 0)                         # un intento anterior quedo a medias: desactivar
    A.str_i(0, 4, 0)
    A.strb_i(0, 4, MB_GUARD)
    A.b("mt_done")
    A.label("mt_go")
    A.ldr_i(5, 4, MB_SEQ)
    A.ldr_i(0, 4, MB_APPLIED)
    A.cmp_r(5, 0)
    A.bcond("eq", "mt_done")
    A.movs(0, 1)
    A.strb_i(0, 4, MB_GUARD)
    A.bl("find_panel")
    A.cmp_i(0, 0)
    A.bcond("eq", "mt_mark")
    A.mov_r(6, 0)                        # panel
    A.mov_r(7, 1)                        # imagen play/pausa
    A.ldrb_i(0, 4, MB_STATE)
    A.cmp_i(0, 1)
    A.bcond("ne", "mt_st2")
    A.addr(1, "dsc_pause")
    A.b("mt_setimg")
    A.label("mt_st2")
    A.cmp_i(0, 2)
    A.bcond("ne", "mt_label")
    A.addr(1, "dsc_play")
    A.label("mt_setimg")
    A.mov_r(0, 7)
    A.bl(F_IMG_SET_SRC)
    A.label("mt_label")
    A.ldr_i(0, 6, 8)                     # spec del panel
    A.ldrh_i(1, 0, 0x28)
    A.cmp_i(1, DEVNAME_IDX + 1)
    A.bcond("lo", "mt_check")
    A.ldr_i0(0, 0)
    A.ldr_i(0, 0, DEVNAME_IDX * 4)       # label del nombre
    A.mov_r(1, 4)
    A.adds_imm8(1, MB_TEXT)
    A.bl(F_LABEL_SET_TEXT)
    A.label("mt_check")
    A.ldr_i(0, 4, MB_SEQ)                # si cambio durante la copia, reintentar
    A.cmp_r(0, 5)
    A.bcond("ne", "mt_unguard")
    A.label("mt_mark")
    A.str_i(5, 4, MB_APPLIED)
    A.label("mt_unguard")
    A.movs(0, 0)
    A.strb_i(0, 4, MB_GUARD)
    A.label("mt_done")
    A.add_sp(4)
    A.pop([4, 5, 6, 7, "pc"], "pop {r4, r5, r6, r7, pc}")

    # ---- datos
    code_end = A.pc
    p = (code_end + 3) & ~3
    layout = {}

    def place(name, blob, align=4):
        nonlocal p
        p = (p + align - 1) & ~(align - 1)
        layout[name] = (p, blob)
        p += len(blob)

    place("title_txt", b"MEDIA\x00")
    place("empty_txt", b"\x00")
    place("codes", bytes(CODES))
    sizes = {"play": 58, "pause": 58, **{n: s for n, s, _ in BUTTONS}, "to_ctl": SWITCH_SIZE, "to_gallery": SWITCH_SIZE}
    icons = {}
    for name in ["play", "pause", "vol_down", "prev", "next", "vol_up"]:
        icons[name] = v2.make_icon(name, sizes[name])
    icons["to_ctl"] = v2.make_icon("play", SWITCH_SIZE)
    icons["to_gallery"] = make_icon_plain("to_gallery", SWITCH_SIZE)
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


def check_sites(mem):
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    img = bytes(mem[a] for a in range(0x08011000, IMG_END))
    for s in SITES:
        ins = list(md.disasm(img[s - 0x08011000:s - 0x08011000 + 4], s))
        assert len(ins) == 1 and ins[0].size == 4, f"{s:#x} no es una instruccion de 32 bits"
    mids = {s + 2 for s in SITES}
    bad = []
    for off in range(0, len(img) - 4, 2):
        for ins in md.disasm(img[off:off + 4], 0x08011000 + off, count=1):
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
    for site, (orig, kind, target) in SITES.items():
        cur = bytes(mem[site + i] for i in range(4))
        ins = list(md.disasm(cur, site))[0]
        if orig is None:
            want_kind, want_t = SITE_ORIG_TARGET[site]
            assert ins.mnemonic == want_kind and int(ins.op_str[1:], 16) == want_t, (hex(site), ins.mnemonic, ins.op_str)
        elif cur.hex() != orig:
            sys.exit(f"bytes inesperados en {site:#x}: {cur.hex()} (esperaba {orig})")
        t = A.labels[target]
        if kind == "bhi.w":
            new = Asm4.bcond32(site, t, "hi")
        else:
            new = Asm.branch32(site, t, kind == "bl")
        chk = list(md.disasm(new, site))[0]
        assert chk.mnemonic == kind and int(chk.op_str[1:], 16) == t, (chk.mnemonic, chk.op_str)
        for i in range(4):
            mem[site + i] = new[i]
        print(f"  {site:#010x}: {ins.mnemonic} {ins.op_str} -> {kind} {target}  ({cur.hex()} -> {new.hex()})")
    edits = dict(v1.IMM_EDITS)
    if persist:
        edits.update(v1.PERSIST_EDIT)
    for site, (old, new, old_txt, new_txt, desc) in edits.items():
        cur = bytes(mem[site + i] for i in range(2))
        assert cur.hex() == old, (hex(site), cur.hex())
        nb = bytes.fromhex(new)
        got = list(md.disasm(nb, site))[0]
        assert f"{got.mnemonic} {got.op_str}" == new_txt
        mem[site], mem[site + 1] = nb[0], nb[1]
        print(f"  {site:#010x}: {desc}  ({old_txt} -> {new_txt})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persist", action="store_true")
    args = ap.parse_args()
    out_dir = ROOT / ("patched-screen3-persist" if args.persist else "patched-screen3")
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
    apply(mem, A, args.persist)

    for idx, start, b in recs:
        if any(mem[start + k] != b[4 + k] for k in range(b[0])):
            rb = bytearray(b)
            for k in range(b[0]):
                rb[4 + k] = mem[start + k]
            rb[-1] = (-sum(rb[:-1])) & 0xFF
            eol = lines[idx][len(lines[idx].rstrip("\r\n")):]
            lines[idx] = ":" + rb.hex().upper() + eol
    assert (CODE >> 16) == ((CODE + len(blob)) >> 16)
    last_idx = recs[-1][0]
    lines[last_idx + 1:last_idx + 1] = [rec((CODE + i) & 0xFFFF, blob[i:i + 16]) for i in range(0, len(blob), 16)]
    out = "".join(lines).encode("ascii")
    out_dir.mkdir(exist_ok=True)
    dst.write_bytes(out)

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
