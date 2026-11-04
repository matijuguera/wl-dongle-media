"""v4 de "Media Control": agrega la pantalla SPOTIFY y saca el doble toque de Media Control.

Sobre v3 (ver make_media_screen3.py):
  * Dos modos del panel (palabra de modo en el buzon de RAM, "SPT1" = Spotify):
      MEDIA:   titulo, bateria, nombre de la salida (abajo), icono galeria (-> Media), icono Spotify,
               5 botones. Tocar el nombre = 0x15 (siguiente salida).
      SPOTIFY: titulo, bateria, volumen de Spotify en el centro, icono volver, - y + grandes
               (0x18 / 0x19). La PC sube/baja la sesion de Spotify.exe en el mezclador.
    El modo se cambia rearmando la pagina 1 con lv_async_call (igual que el firmware).
  * Comando 0x20: [6]=estado, [7..46]=texto 1 (salida de audio), [47..86]=texto 2 (Spotify).
  * El panel se identifica por su fondo: una copia propia del descriptor del fondo del menu (puntero
    unico), asi la busqueda funciona en los dos modos.
  * is_link_active (0x080242BC) devuelve 0 en Media Control: sin doble toque ni demora del toque simple.
  * Guard de armado: si armar el modo Spotify se cuelga y el dongle se reinicia (la RAM sobrevive a un
    reinicio en caliente), el siguiente armado vuelve al modo MEDIA en vez de reintentar.

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
import make_media_screen3 as v3
from make_media_button import Asm, parse_hex, rec, verify
from make_media_screen3 import Asm4

ROOT = pathlib.Path(__file__).resolve().parent.parent
CODE = 0x08119020
IMG_END = base.IMG_END

# firmware (ver v3)
F_SPI_SEND, F_P1_CTOR, F_MEDIA_CTOR = v3.F_SPI_SEND, v3.F_P1_CTOR, v3.F_MEDIA_CTOR
F_IDX_GET, F_IDX_SET, F_SCREEN_ACTIVE = v3.F_IDX_GET, v3.F_IDX_SET, v3.F_SCREEN_ACTIVE
F_OBJ_CREATE, F_REMOVE_STYLE_ALL, F_SET_SIZE = v3.F_OBJ_CREATE, v3.F_REMOVE_STYLE_ALL, v3.F_SET_SIZE
F_IMG_CREATE, F_IMG_SET_SRC, F_SET_POS = v3.F_IMG_CREATE, v3.F_IMG_SET_SRC, v3.F_SET_POS
F_TINT, F_REMOVE_FLAG, F_LABEL_SET_TEXT = v3.F_TINT, v3.F_REMOVE_FLAG, v3.F_LABEL_SET_TEXT
F_BATT_WIDGET, F_BATT_WIDGET2, F_STATUS_UPDATE = v3.F_BATT_WIDGET, v3.F_BATT_WIDGET2, v3.F_STATUS_UPDATE
F_ASYNC_CALL, F_LOOP_ORIG = v3.F_ASYNC_CALL, v3.F_LOOP_ORIG
THUNK_MEDIA, THUNK_P1 = v3.THUNK_MEDIA, v3.THUNK_P1
CMD_UNKNOWN, CMD_OK = v3.CMD_UNKNOWN, v3.CMD_OK
MENU_BG_DSC, FONT_TITLE, FONT_NAME = v3.MENU_BG_DSC, v3.FONT_TITLE, v3.FONT_NAME
CFG_BYTE1, STATE, RAW_DEFAULT = v3.CFG_BYTE1, v3.STATE, v3.RAW_DEFAULT
LINK_ACTIVE = 0x080242BC

MBOX = 0x2011D400
MAGIC = 0x3144454D             # "MED1"
MODE_SPOTIFY = 0x31545053      # "SPT1"
MB_SEQ, MB_APPLIED, MB_STATE, MB_GUARD, MB_BUILD = 4, 8, 12, 13, 14
MB_TEXT1, MB_TEXT2, MB_MODE = 16, 56, 96
TEXT_LEN = 40                  # cada texto: 39 caracteres + 0

PAGE_Y = 35
BTN_TOP, NAME_TOP = 40, 126
CODES = [0x14, 0x12, 0x10, 0x11, 0x13]
CODE_OUTPUT, CODE_SPOT_DOWN, CODE_SPOT_UP = 0x15, 0x18, 0x19
BUTTONS = [("vol_down", 44, 32), ("prev", 48, 96), ("play", 58, 160), ("next", 48, 224), ("vol_up", 44, 288)]
ROW_CY = 86
SMALL, SMALL_Y = 28, 6
GALLERY_X, SPOTIFY_X = 8, 44
BIG = 64
INFO_IDX, PP_IDX = 4, 9        # hijos del panel MEDIA: 0 fondo,1-2 bateria,3 titulo,4 info,5-6 iconos,7-11 botones

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
    0x080242BC: ("41f2f050", "b.w", "hook_link"),   # movw r0, #0x15f0
}
SITE_ORIG_TARGET = v3.SITE_ORIG_TARGET


# ------------------------------------------------------------------ iconos nuevos
def spotify_glyph(u, v):
    """Tres arcos tipo logo de Spotify (se calan del disco)."""
    for base_v, half_w, thick in ((0.34, 0.29, 0.075), (0.49, 0.25, 0.065), (0.63, 0.21, 0.055)):
        d = u - 0.5
        if abs(d) <= half_w and abs(v - (base_v + 0.55 * d * d)) <= thick / 2:
            return True
    return False


def back_glyph(u, v):
    return v2._tri((u, v), (0.62, 0.28), (0.62, 0.72), (0.30, 0.50))


def make_disc_icon(glyph, size):
    S = 4
    out = bytearray()
    for py in range(size):
        for px in range(size):
            acc = 0
            for sy in range(S):
                for sx in range(S):
                    u = (px + (sx + 0.5) / S) / size
                    v = (py + (sy + 0.5) / S) / size
                    if (u - 0.5) ** 2 + (v - 0.5) ** 2 <= 0.47 ** 2 and not glyph(u, v):
                        acc += 1
            out.append(round(235 * acc / (S * S)))
    return bytes(out)


# ------------------------------------------------------------------ codigo
def build():
    A = Asm4(CODE)

    def is_spotify(reg, tmp):
        """flags: eq si el modo es Spotify (pisa reg y tmp)."""
        A.mov32(reg, MBOX)
        A.ldr_i(reg, reg, MB_MODE)
        A.mov32(tmp, MODE_SPOTIFY)
        A.cmp_r(reg, tmp)

    # ---- hook_cmd (tarea de transporte). Primero por el rango de bhi.w.
    A.label("hook_cmd")
    A.cmp_i(1, 0x20)
    A.bcond("eq", "hc_go")
    A.bw(CMD_UNKNOWN)
    A.label("hc_go")
    A.mov32(2, MBOX)
    A.mov_r(3, 8)
    A.ldrb_i(0, 3, 6)
    A.strb_i(0, 2, MB_STATE)
    A.adds_imm8(3, 7)
    A.movs(4, 0)
    A.label("hc_loop")                   # copiar 80 bytes: texto 1 y texto 2
    A.ldrb_r(5, 3, 4)
    A.adds_rrr(6, 2, 4)
    A.strb_i(5, 6, MB_TEXT1)
    A.adds_imm8(4, 1)
    A.cmp_i(4, 2 * TEXT_LEN)
    A.bcond("lo", "hc_loop")
    A.movs(5, 0)                         # terminadores fijos
    A.mov_r(6, 2)
    A.adds_imm8(6, TEXT_LEN - 1)
    A.strb_i(5, 6, MB_TEXT1)
    A.adds_imm8(6, TEXT_LEN)
    A.strb_i(5, 6, MB_TEXT1)
    A.ldr_i(0, 2, MB_SEQ)
    A.adds_imm8(0, 1)
    A.str_i(0, 2, MB_SEQ)
    A.mov32(0, MAGIC)
    A.str_i(0, 2, 0)
    A.bw(CMD_OK)

    # ---- hook_link: is_link_active() -> 0 en Media Control (pagina 1 con indice 3)
    A.label("hook_link")
    A.mov32(1, STATE)
    A.ldrb_i(1, 1, 0xC)
    A.cmp_i(1, 1)
    A.bcond("ne", "hl_orig")
    A.mov32(1, CFG_BYTE1)
    A.ldrb_i(1, 1, 0)
    A.and_imm8(1, 1, 3)
    A.cmp_i(1, 3)
    A.bcond("ne", "hl_orig")
    A.movs(0, 0)
    A.raw("7047", "bx lr")
    A.label("hl_orig")
    A.movw(0, 0x15F0)
    A.bw(LINK_ACTIVE + 4)

    # ---- hook_tap: r0=y r1=0 r2=x r3=pagina r5=estado r7=1<<pagina; r6 libre
    A.label("hook_tap")
    A.cmp_i(3, 2)
    A.bcond("eq", "ht_media_pg")
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
    is_spotify(0, 1)                     # pisa r0/r1: para volver al original usar tap_orig_r
    A.bcond("eq", "ht_spot")
    # --- modo MEDIA
    A.cmp_i(6, BTN_TOP)
    A.bcond("hs", "ht_lower")
    A.cmp_i(2, 40)
    A.bcond("lo", "ht_gallery")
    A.cmp_i(2, 80)
    A.bcond("hs", "tap_orig_r")
    A.movs(0, 1)
    A.bl("switch_mode")
    A.b("ht_ret")
    A.label("ht_gallery")
    A.movs(0, 2)
    A.bl("switch_to")
    A.b("ht_ret")
    A.label("ht_lower")
    A.movw(1, 319)
    A.cmp_r(2, 1)
    A.bcond("hi", "tap_orig_r")
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
    # --- modo SPOTIFY
    A.label("ht_spot")
    A.cmp_i(6, BTN_TOP)
    A.bcond("hs", "hs_lower")
    A.cmp_i(2, 56)
    A.bcond("hs", "tap_orig_r")
    A.movs(0, 0)
    A.bl("switch_mode")
    A.b("ht_ret")
    A.label("hs_lower")
    A.movs(1, CODE_SPOT_DOWN)
    A.cmp_i(2, 160)
    A.bcond("lo", "hs_send")
    A.movs(1, CODE_SPOT_UP)
    A.label("hs_send")
    A.bl("send_code")
    A.b("ht_ret")
    # --- pagina Media (galeria): icono chico -> Media Control
    A.label("ht_media_pg")
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
    A.label("tap_orig_r")                # restaurar lo que el original espera: r0 = y, r1 = 0
    A.mov_r(0, 6)
    A.adds_imm8(0, PAGE_Y)
    A.movs(1, 0)
    A.label("tap_orig")
    A.movw(6, 0x806)
    A.bw(0x0801E57C)

    # ---- send_code(r1 = codigo)
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

    # ---- rebuild_p1(): arma de nuevo la pagina 1 de forma diferida (como al volver del menu)
    A.label("rebuild_p1")
    A.push([4, 5, 6, "lr"], "push {r4, r5, r6, lr}")
    A.mov32(5, STATE)
    A.mov32(0, THUNK_P1)
    A.label("rb_go")
    A.movs(1, 0)
    A.strb_i(1, 5, 2)
    A.movs(1, 1)
    A.strb_i(1, 5, 4)
    A.movs(1, 0)
    A.bl(F_ASYNC_CALL)
    A.cmp_i(0, 1)
    A.bcond("eq", "rb_done")
    A.movs(0, 0)
    A.strb_i(0, 5, 4)
    A.label("rb_done")
    A.pop([4, 5, 6, "pc"], "pop {r4, r5, r6, pc}")

    # ---- switch_mode(r0 = 0 MEDIA / 1 SPOTIFY)
    A.label("switch_mode")
    A.push([4, "lr"], "push {r4, lr}")
    A.mov32(1, MBOX)
    A.movs(2, 0)
    A.cmp_i(0, 0)
    A.bcond("eq", "sm_set")
    A.mov32(2, MODE_SPOTIFY)
    A.label("sm_set")
    A.str_i(2, 1, MB_MODE)
    A.bl("rebuild_p1")
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
    A.mov32(0, THUNK_P1)
    A.cmp_i(4, 2)
    A.bcond("ne", "rb_go")              # comparte la cola con rebuild_p1 (mismo frame y registros)
    A.mov32(0, THUNK_MEDIA)
    A.b("rb_go")

    # ---- hook_map: cmd 0x0A, valor 4 -> indice 3
    A.label("hook_map")
    A.cmp_i(0, 4)
    A.bcond("ne", "hm_orig")
    A.movs(1, 3)
    A.bw(0x08024A5E)
    A.label("hm_orig")
    A.raw("22fa01f1", "lsr.w r1, r2, r1")
    A.bw(0x08024A5E)

    # ---- p1_ctor
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

    # ---- media_ctor: galeria + icono chico (-> Media Control)
    A.label("media_ctor")
    A.push([4, "lr"], "push {r4, lr}")
    A.bl(F_MEDIA_CTOR)
    A.mov_r(4, 0)
    A.bl(F_SCREEN_ACTIVE)
    A.cmp_i(0, 0)
    A.bcond("eq", "mc_done")
    A.addr(1, "dsc_to_ctl")
    A.movs(2, GALLERY_X)
    A.movs(3, PAGE_Y + SMALL_Y)
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
    A.mov_r(6, 0)
    # guard de armado: si el armado anterior no termino, volver a MEDIA
    A.mov32(4, MBOX)
    A.ldrb_i(0, 4, MB_BUILD)
    A.cmp_i(0, 0)
    A.bcond("eq", "ap_guard_ok")
    A.movs(0, 0)
    A.str_i(0, 4, MB_MODE)
    A.label("ap_guard_ok")
    A.movs(0, 1)
    A.strb_i(0, 4, MB_BUILD)
    A.mov_r(0, 6)
    A.bl(F_OBJ_CREATE)
    A.cmp_i(0, 0)
    A.bcond_w("eq", "ap_end")
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
    A.mov_r(0, 5)                        # 0: fondo (descriptor propio = marca del panel)
    A.bl(F_IMG_CREATE)
    A.cmp_i(0, 0)
    A.bcond_w("eq", "ap_end")
    A.mov_r(4, 0)
    A.addr(1, "dsc_panel_bg")
    A.bl(F_IMG_SET_SRC)
    A.mov_r(0, 4)
    A.movs(1, 0)
    A.movs(2, 0)
    A.bl(F_SET_POS)
    A.mov_r(0, 4)
    A.movs(1, 0x12)
    A.bl(F_REMOVE_FLAG)
    A.mov_r(0, 5)                        # 1-2: bateria
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
    is_spotify(0, 1)
    A.bcond_w("eq", "ap_spot")
    # --- MEDIA
    A.label_call(5, "title_media", FONT_TITLE, 0, 6, 320, 26)          # 3
    A.label_call(5, "empty_txt", FONT_NAME, 0, 132, 320, 24)           # 4: salida de audio
    for dsc, x in (("dsc_to_gallery", GALLERY_X), ("dsc_spotify", SPOTIFY_X)):   # 5, 6
        A.mov_r(0, 5)
        A.addr(1, dsc)
        A.movs(2, x)
        A.movs(3, SMALL_Y)
        A.bl("put_icon")
    for name, size, cx in BUTTONS:                                       # 7..11
        A.mov_r(0, 5)
        A.addr(1, f"dsc_{name}")
        A.movw(2, cx - size // 2)
        A.movs(3, ROW_CY - size // 2)
        A.bl("put_icon")
    A.b("ap_common")
    # --- SPOTIFY
    A.label("ap_spot")
    A.label_call(5, "title_spotify", FONT_TITLE, 0, 6, 320, 26)        # 3
    A.label_call(5, "empty_txt", FONT_TITLE, 96, ROW_CY - 13, 128, 26) # 4: volumen de Spotify
    A.mov_r(0, 5)                                                        # 5: volver
    A.addr(1, "dsc_back")
    A.movs(2, GALLERY_X)
    A.movs(3, SMALL_Y)
    A.bl("put_icon")
    for dsc, cx in (("dsc_big_down", 60), ("dsc_big_up", 260)):         # 6, 7
        A.mov_r(0, 5)
        A.addr(1, dsc)
        A.movw(2, cx - BIG // 2)
        A.movs(3, ROW_CY - BIG // 2)
        A.bl("put_icon")
    A.label("ap_common")
    A.mov32(0, MBOX)                     # que my_tick reaplique los textos
    A.ldr_i(1, 0, MB_SEQ)
    A.subs_imm8(1, 1)
    A.str_i(1, 0, MB_APPLIED)
    A.bl(F_STATUS_UPDATE)
    A.label("ap_end")
    A.mov32(0, MBOX)                     # armado terminado
    A.movs(1, 0)
    A.strb_i(1, 0, MB_BUILD)
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

    # ---- find_panel() -> r0 = panel (o 0), r1 = arreglo de hijos, r2 = cantidad de hijos
    A.label("find_panel")
    A.push([4, 5, 6, 7, "lr"], "push {r4, r5, r6, r7, lr}")
    A.sub_sp(4)
    A.addr(5, "dsc_panel_bg")
    A.bl(F_SCREEN_ACTIVE)
    A.cmp_i(0, 0)
    A.bcond("eq", "fp_none")
    A.ldr_i(1, 0, 8)
    A.cmp_i(1, 0)
    A.bcond("eq", "fp_none")
    A.ldrh_i(2, 1, 0x28)
    A.ldr_i0(1, 1)
    A.label("fp_loop")
    A.subs_imm8(2, 1)
    A.bcond("mi", "fp_none")
    A.lsls_i(3, 2, 2)
    A.add_r(3, 1)
    A.ldr_i0(3, 3)                       # hijo c de la pantalla
    A.ldr_i(4, 3, 8)                     # c->spec_attr
    A.cmp_i(4, 0)
    A.bcond("eq", "fp_loop")
    A.ldrh_i(6, 4, 0x28)                 # cantidad de hijos de c
    A.cmp_i(6, 1)
    A.bcond("lo", "fp_loop")
    A.ldr_i0(4, 4)                       # hijos de c
    A.ldr_i0(7, 4)                       # hijo 0
    A.ldr_i(7, 7, 0x2C)                  # su src
    A.cmp_r(7, 5)
    A.bcond("ne", "fp_loop")
    A.mov_r(0, 3)
    A.mov_r(1, 4)
    A.mov_r(2, 6)
    A.b("fp_ret")
    A.label("fp_none")
    A.movs(0, 0)
    A.movs(1, 0)
    A.movs(2, 0)
    A.label("fp_ret")
    A.add_sp(4)
    A.pop([4, 5, 6, 7, "pc"], "pop {r4, r5, r6, r7, pc}")

    # ---- toggle_pp: solo en modo MEDIA, hijo PP_IDX
    A.label("toggle_pp")
    A.push([4, "lr"], "push {r4, lr}")
    A.bl("find_panel")
    A.cmp_i(0, 0)
    A.bcond("eq", "tp_done")
    A.cmp_i(2, PP_IDX + 1)
    A.bcond("lo", "tp_done")
    A.ldr_i(4, 1, PP_IDX * 4)
    A.ldr_i(0, 4, 0x2C)
    A.addr(1, "dsc_play")
    A.cmp_r(0, 1)
    A.bcond("eq", "tp_to_pause")
    A.addr(2, "dsc_pause")
    A.cmp_r(0, 2)
    A.bcond("ne", "tp_done")             # no es el boton play/pausa (modo Spotify)
    A.b("tp_set")
    A.label("tp_to_pause")
    A.addr(1, "dsc_pause")
    A.label("tp_set")
    A.mov_r(0, 4)
    A.bl(F_IMG_SET_SRC)
    A.label("tp_done")
    A.pop([4, "pc"], "pop {r4, pc}")

    # ---- my_tick (tarea LVGL)
    A.label("my_tick")
    A.push([4, 5, 6, 7, "lr"], "push {r4, r5, r6, r7, lr}")
    A.sub_sp(4)
    A.bl(F_LOOP_ORIG)
    A.mov32(4, MBOX)
    A.ldr_i(0, 4, 0)
    A.mov32(1, MAGIC)
    A.cmp_r(0, 1)
    A.bcond_w("ne", "mt_done")
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
    A.mov_r(6, 1)                        # hijos del panel
    A.mov_r(7, 2)                        # cantidad
    A.cmp_i(7, INFO_IDX + 1)
    A.bcond("lo", "mt_check")
    is_spotify(0, 1)
    A.bcond("eq", "mt_spot")
    # MEDIA: estado de reproduccion (si viene) + texto 1
    A.cmp_i(7, PP_IDX + 1)
    A.bcond("lo", "mt_t1")
    A.ldrb_i(0, 4, MB_STATE)
    A.cmp_i(0, 1)
    A.bcond("ne", "mt_st2")
    A.addr(1, "dsc_pause")
    A.b("mt_setimg")
    A.label("mt_st2")
    A.cmp_i(0, 2)
    A.bcond("ne", "mt_t1")
    A.addr(1, "dsc_play")
    A.label("mt_setimg")
    A.ldr_i(0, 6, PP_IDX * 4)
    A.bl(F_IMG_SET_SRC)
    A.label("mt_t1")
    A.mov_r(1, 4)
    A.adds_imm8(1, MB_TEXT1)
    A.b("mt_settext")
    A.label("mt_spot")
    A.mov_r(1, 4)
    A.adds_imm8(1, MB_TEXT2)
    A.label("mt_settext")
    A.ldr_i(0, 6, INFO_IDX * 4)
    A.bl(F_LABEL_SET_TEXT)
    A.label("mt_check")
    A.ldr_i(0, 4, MB_SEQ)
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

    place("title_media", b"MEDIA\x00")
    place("title_spotify", b"SPOTIFY\x00")
    place("empty_txt", b"\x00")
    place("codes", bytes(CODES))
    # copia del descriptor del fondo del menu (mismos datos, puntero propio)
    bg = bytes(base_mem_dsc(MENU_BG_DSC))
    place("dsc_panel_bg", bg)
    sizes = {"play": 58, "pause": 58, **{n: s for n, s, _ in BUTTONS},
             "to_ctl": SMALL, "to_gallery": SMALL, "spotify": SMALL, "back": SMALL, "big_down": BIG, "big_up": BIG}
    icons = {}
    for name in ["play", "pause", "vol_down", "prev", "next", "vol_up"]:
        icons[name] = v2.make_icon(name, sizes[name])
    icons["to_ctl"] = v2.make_icon("play", SMALL)
    icons["to_gallery"] = v3.make_icon_plain("to_gallery", SMALL)
    icons["spotify"] = make_disc_icon(spotify_glyph, SMALL)
    icons["back"] = make_disc_icon(back_glyph, SMALL)
    icons["big_down"] = v2.make_icon("vol_down", BIG)
    icons["big_up"] = v2.make_icon("vol_up", BIG)
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


_MEM = {}


def base_mem_dsc(addr):
    """24 bytes del descriptor original en la imagen (para copiarlo)."""
    return bytes(_MEM[addr + i] for i in range(24))


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
            assert ins.mnemonic == want_kind and int(ins.op_str[1:], 16) == want_t, (hex(site), ins.op_str)
        elif cur.hex() != orig:
            sys.exit(f"bytes inesperados en {site:#x}: {cur.hex()} (esperaba {orig})")
        t = A.labels[target]
        new = Asm4.bcond32(site, t, "hi") if kind == "bhi.w" else Asm.branch32(site, t, kind == "bl")
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
        assert f"{list(md.disasm(nb, site))[0].mnemonic} {list(md.disasm(nb, site))[0].op_str}" == new_txt
        mem[site], mem[site + 1] = nb[0], nb[1]
        print(f"  {site:#010x}: {desc}  ({old_txt} -> {new_txt})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persist", action="store_true")
    args = ap.parse_args()
    out_dir = ROOT / ("patched-screen4-persist" if args.persist else "patched-screen4")
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
    _MEM.update(mem)
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
