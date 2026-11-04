"""v6: pantallas AUDIO y MEZCLADOR. Sobre v5: % de bateria, nombre de la cancion que se desliza
(solo se reinicia cuando cambia la cancion), botones centrados entre cancion y salida, y sin
"modo reposo" en Media Control (no cambia a la pantalla WLMOUSE ni ignora toques si el mouse duerme).

Descripcion v5:

AUDIO (Default Screen = 4, "Media Control"):
  arriba: [galeria][mezclador]  AUDIO  [microfono] bateria
  ♪ Artista - Cancion (centrado, con "…" si no entra)
  [vol-] [anterior] [play/pausa] [siguiente] [vol+]
  pildora con 🔊 + salida de audio (tocarla cambia de salida)
MEZCLADOR:
  arriba: [volver]  MEZCLADOR  bateria
  ◀ [logo] App ▶  +  puntitos (cual app de cuantas)
  [-]   42% / barra   [+]

El dongle es un "renderizador": la PC manda (cmd LCD 0x20) un bloque de 192 bytes con textos,
estados y posiciones ya calculadas; el loop principal (tarea LVGL) los aplica. Toques -> codigos
`04 CD xx` a la PC: 10-14 multimedia, 15 salida, 18/19 vol app, 1A mic, 1B/1C app anterior/siguiente,
1D se abrio el mezclador (la PC elige Spotify), 1E mute de la app.

Base: hex con el doble toque parcheado. Mismos hooks que v4 (ver make_media_screen4.py). Solo escribe
archivos; no habla con el dongle.
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
import make_media_screen4 as v4
from make_media_button import Asm, parse_hex, verify
from make_media_screen3 import Asm4, imm_txt

ROOT = pathlib.Path(__file__).resolve().parent.parent
CODE = 0x08119020
IMG_END = base.IMG_END

F_SPI_SEND, F_P1_CTOR, F_MEDIA_CTOR = v3.F_SPI_SEND, v3.F_P1_CTOR, v3.F_MEDIA_CTOR
F_IDX_GET, F_IDX_SET, F_SCREEN_ACTIVE = v3.F_IDX_GET, v3.F_IDX_SET, v3.F_SCREEN_ACTIVE
F_OBJ_CREATE, F_REMOVE_STYLE_ALL, F_SET_SIZE = v3.F_OBJ_CREATE, v3.F_REMOVE_STYLE_ALL, v3.F_SET_SIZE
F_IMG_CREATE, F_IMG_SET_SRC, F_SET_POS = v3.F_IMG_CREATE, v3.F_IMG_SET_SRC, v3.F_SET_POS
F_REMOVE_FLAG, F_LABEL_SET_TEXT, F_LABEL = v3.F_REMOVE_FLAG, v3.F_LABEL_SET_TEXT, v3.F_LABEL
F_BATT_WIDGET, F_BATT_WIDGET2, F_STATUS_UPDATE = v3.F_BATT_WIDGET, v3.F_BATT_WIDGET2, v3.F_STATUS_UPDATE
F_ASYNC_CALL, F_LOOP_ORIG = v3.F_ASYNC_CALL, v3.F_LOOP_ORIG
F_ADD_FLAG = 0x08062AC0          # lv_obj_add_flag(obj, flag)
F_COLOR_HEX = 0x0803E8C4         # lv_color_hex(0xRRGGBB)
F_STYLE_RECOLOR = 0x08068EE0     # (obj, color, selector)
F_STYLE_RECOLOR_OPA = 0x08068EEC # (obj, opa, selector)
F_LABEL_LONG_MODE = 0x08060CD4   # (label, mode) 1 = puntos suspensivos
THUNK_MEDIA, THUNK_P1 = v3.THUNK_MEDIA, v3.THUNK_P1
CMD_UNKNOWN, CMD_OK = v3.CMD_UNKNOWN, v3.CMD_OK
MENU_BG_DSC, CFG_BYTE1, STATE, RAW_DEFAULT = v3.MENU_BG_DSC, v3.CFG_BYTE1, v3.STATE, v3.RAW_DEFAULT
LINK_ACTIVE = v4.LINK_ACTIVE
FONT18 = 0x080A3710
TEXT_COLOR = 0x00F1F0EC

ACCENT, DARK, RED, WHITE, GRAY = 0xB765FF, 0x2A2833, 0xE24B4A, 0xF1F0EC, 0x4A4855

MBOX = 0x2011D400
MAGIC = 0x3344454D               # "MED3"
MODE_MIXER = 0x3158494D          # "MIX1"
MB_SEQ, MB_APPLIED, MB_MODE, MB_GUARD, MB_BUILD, MB_BLOCK = 4, 8, 12, 16, 17, 32
MB_CACHE = 240                   # copia de la ultima cancion mostrada (64 bytes)
BLOCK_LEN = 208
# bloque (offsets desde MB_BLOCK)
B_PLAY, B_MIC, B_APPICON, B_DOTS, B_DOTIDX = 0, 1, 2, 3, 4
B_PILLW, B_APPX, B_DOTX, B_BARW = 6, 8, 10, 12
B_SONG, B_OUT, B_APP, B_PCT, B_BATT = 16, 80, 128, 176, 192
TEXT_ENDS = (B_OUT - 1, B_APP - 1, B_PCT - 1, B_BATT - 1, BLOCK_LEN - 1)

PAGE_Y = 35
CODES = [0x14, 0x12, 0x10, 0x11, 0x13]
C_OUTPUT, C_APP_DOWN, C_APP_UP, C_MIC, C_APP_PREV, C_APP_NEXT, C_MIXER_OPEN, C_APP_MUTE = \
    0x15, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E

# AUDIO: (nombre, tamano, x, y)
# centro vertical 97: mismo margen (13 px) entre la cancion (termina en 55) y la pildora (empieza en 139)
BUTTONS = [("vol_down", 44, 9, 75), ("prev", 48, 71, 73), ("play", 58, 131, 68), ("next", 48, 201, 73), ("vol_up", 44, 267, 75)]
SMALL = 26
PILL_W, PILL_H, PILL_Y, CAP_W = 300, 24, 139, 12
BAR_X, BAR_Y, BAR_W, BAR_H = 74, 106, 172, 8
DOT, DOT_Y, DOT_STEP, MAX_DOTS = 6, 60, 11, 6
APP_ICON, APP_ICON_Y = 18, 39
BIG = 52
# indices de hijos del panel
A_SONG, A_PILL, A_OUT, A_MICDISC, A_MIC, A_PP = 4, 5, 6, 11, 12, 15
M_APP, M_ICON, M_DOT0, M_BAR, M_PCT = 4, 5, 6, 12, 13
I_BATT = 18                       # en los dos modos: % de bateria
MIC_X = 236
FONT11 = 0x080A36C8
SLEEP_SITE = 0x08022578          # ldrb.w r0, [sp, #0x49] (flag "mouse dormido")

SITES = dict(v4.SITES)
SITES[SLEEP_SITE] = ("9df84900", "b.w", "hook_sleep")
SITE_ORIG_TARGET = v4.SITE_ORIG_TARGET


class Asm5(Asm4):
    def uxth(self, rd, rm):
        self.h(0xB280 | rm << 3 | rd, f"uxth r{rd}, r{rm}")

    def strb_r(self, rt, rn, rm):
        self.h(0x5400 | rm << 6 | rn << 3 | rt, f"strb r{rt}, [r{rn}, r{rm}]")

    def ldrh_i0(self, rt, rn, imm):
        self.h(0x8800 | (imm // 2) << 6 | rn << 3 | rt, f"ldrh r{rt}, [r{rn}]" if imm == 0 else f"ldrh r{rt}, [r{rn}, {imm_txt(imm)}]")

    def ldrb_i0(self, rt, rn, imm):
        self.h(0x7800 | imm << 6 | rn << 3 | rt, f"ldrb r{rt}, [r{rn}]" if imm == 0 else f"ldrb r{rt}, [r{rn}, {imm_txt(imm)}]")

    def subs_rrr(self, rd, rn, rm):
        self.h(0x1A00 | rm << 6 | rn << 3 | rd, f"subs r{rd}, r{rn}, r{rm}")

    def imm(self, rd, v):
        """cargar una constante en rd con la instruccion mas corta."""
        if 0 <= v <= 255:
            self.movs(rd, v)
        elif v <= 0xFFFF:
            self.movw(rd, v)
        else:
            self.mov32(rd, v)


# ------------------------------------------------------------------ iconos (A8)
def _in_poly(p, *vs):
    return any(v2._tri(p, vs[0], vs[i], vs[i + 1]) for i in range(1, len(vs) - 1))


def _rr(u, v, x0, x1, y0, y1, r):  # rectangulo redondeado
    if not (x0 <= u <= x1 and y0 <= v <= y1):
        return False
    cx = min(max(u, x0 + r), x1 - r)
    cy = min(max(v, y0 + r), y1 - r)
    return (u - cx) ** 2 + (v - cy) ** 2 <= r * r


def g_minus(u, v):
    return 0.28 <= u <= 0.72 and 0.455 <= v <= 0.545


def g_plus(u, v):
    return g_minus(u, v) or (0.455 <= u <= 0.545 and 0.28 <= v <= 0.72)


def g_gallery(u, v):
    p = (u, v)
    frame = _rr(u, v, 0.22, 0.78, 0.28, 0.72, 0.06) and not (0.29 <= u <= 0.71 and 0.35 <= v <= 0.65)
    return frame or _in_poly(p, (0.29, 0.65), (0.45, 0.45), (0.58, 0.65)) or (u - 0.61) ** 2 + (v - 0.44) ** 2 <= 0.05 ** 2


def g_mixer(u, v):
    lines = any(abs(u - x) <= 0.03 and 0.26 <= v <= 0.74 for x in (0.33, 0.5, 0.67))
    knobs = any((u - x) ** 2 + (v - y) ** 2 <= 0.075 ** 2 for x, y in ((0.33, 0.6), (0.5, 0.38), (0.67, 0.55)))
    return lines or knobs


def g_mic(u, v):
    body = _rr(u, v, 0.41, 0.59, 0.24, 0.58, 0.09)
    d = ((u - 0.5) ** 2 + (v - 0.5) ** 2) ** 0.5
    cup = 0.15 <= d <= 0.20 and v >= 0.5
    stem = abs(u - 0.5) <= 0.025 and 0.68 <= v <= 0.77
    base_ = 0.40 <= u <= 0.60 and 0.75 <= v <= 0.79
    return body or cup or stem or base_


def g_mic_off(u, v):
    slash = abs((v - 0.22) - (u - 0.22)) <= 0.04 and 0.22 <= u <= 0.80
    return g_mic(u, v) or slash


def g_back(u, v):
    return any(abs((v - 0.5) - s * (u - 0.40)) <= 0.05 and 0.40 <= u <= 0.60 and (s * (v - 0.5) >= 0)
               for s in (1, -1)) and abs(v - 0.5) <= 0.2


def g_app_generic(u, v):
    p = (u, v)
    return _rr(u, v, 0.15, 0.32, 0.38, 0.62, 0.02) or _in_poly(p, (0.30, 0.38), (0.55, 0.18), (0.55, 0.82), (0.30, 0.62)) \
        or (abs(((u - 0.55) ** 2 + (v - 0.5) ** 2) ** 0.5 - 0.27) <= 0.04 and u > 0.62)


def g_app_discord(u, v):
    body = _rr(u, v, 0.12, 0.88, 0.25, 0.78, 0.18)
    eyes = any((u - x) ** 2 + (v - 0.52) ** 2 <= 0.075 ** 2 for x in (0.37, 0.63))
    return body and not eyes


def g_app_browser(u, v):
    d = ((u - 0.5) ** 2 + (v - 0.5) ** 2) ** 0.5
    ring = 0.38 <= d <= 0.46
    mer = abs(((u - 0.5) / 0.18) ** 2 + ((v - 0.5) / 0.42) ** 2 - 1) <= 0.25 and d <= 0.42
    eq = abs(v - 0.5) <= 0.035 and d <= 0.42
    return ring or mer or eq


def g_app_game(u, v):
    body = _rr(u, v, 0.08, 0.92, 0.28, 0.74, 0.18)
    cross = (0.22 <= u <= 0.40 and 0.47 <= v <= 0.55) or (0.27 <= u <= 0.35 and 0.42 <= v <= 0.60)
    btn = any((u - x) ** 2 + (v - y) ** 2 <= 0.05 ** 2 for x, y in ((0.66, 0.45), (0.76, 0.56)))
    return body and not (cross or btn)


def render(fn, w, h=None, alpha=255):
    h = h or w
    S = 4
    out = bytearray()
    for py in range(h):
        for px in range(w):
            acc = 0
            for sy in range(S):
                for sx in range(S):
                    if fn((px + (sx + 0.5) / S) / w, (py + (sy + 0.5) / S) / h):
                        acc += 1
            out.append(round(alpha * acc / (S * S)))
    return bytes(out)


def disc_cut(glyph):
    return lambda u, v: (u - 0.5) ** 2 + (v - 0.5) ** 2 <= 0.47 ** 2 and not glyph(u, v)


def build_icons():
    ic = {}
    for name, size, _, _ in BUTTONS:
        ic[name] = (size, size, v2.make_icon(name, size))
    ic["pause"] = (58, 58, v2.make_icon("pause", 58))
    ic["minus"] = (BIG, BIG, render(disc_cut(g_minus), BIG, alpha=235))
    ic["plus"] = (BIG, BIG, render(disc_cut(g_plus), BIG, alpha=235))
    ic["disc26"] = (SMALL, SMALL, render(lambda u, v: (u - .5) ** 2 + (v - .5) ** 2 <= 0.49 ** 2, SMALL))
    for name, fn in (("gallery", g_gallery), ("mixer", g_mixer), ("mic", g_mic), ("mic_off", g_mic_off), ("back", g_back)):
        ic[name] = (SMALL, SMALL, render(fn, SMALL))
    ic["app0"] = (APP_ICON, APP_ICON, render(g_app_generic, APP_ICON))
    ic["app1"] = (APP_ICON, APP_ICON, render(lambda u, v: (u - .5) ** 2 + (v - .5) ** 2 <= 0.47 ** 2 and not v4.spotify_glyph(u, v), APP_ICON))
    ic["app2"] = (APP_ICON, APP_ICON, render(g_app_discord, APP_ICON))
    ic["app3"] = (APP_ICON, APP_ICON, render(g_app_browser, APP_ICON))
    ic["app4"] = (APP_ICON, APP_ICON, render(g_app_game, APP_ICON))
    ic["dot"] = (DOT, DOT, render(lambda u, v: (u - .5) ** 2 + (v - .5) ** 2 <= 0.48 ** 2, DOT))
    r = PILL_H / 2 / PILL_W
    ic["pill"] = (PILL_W, PILL_H, render(lambda u, v: _rr(u * PILL_W, v * PILL_H, 0, PILL_W + 50, 0, PILL_H, PILL_H / 2), PILL_W, PILL_H))
    ic["cap"] = (CAP_W, PILL_H, render(lambda u, v: _rr(u * CAP_W, v * PILL_H, -50, CAP_W, 0, PILL_H, PILL_H / 2), CAP_W, PILL_H))
    ic["bar"] = (BAR_W, BAR_H, render(lambda u, v: _rr(u * BAR_W, v * BAR_H, 0, BAR_W, 0, BAR_H, BAR_H / 2), BAR_W, BAR_H))
    return ic


# ------------------------------------------------------------------ codigo
def build():
    A = Asm5(CODE)

    def is_mixer(reg, tmp):
        A.mov32(reg, MBOX)
        A.ldr_i(reg, reg, MB_MODE)
        A.mov32(tmp, MODE_MIXER)
        A.cmp_r(reg, tmp)

    # ---- hook_cmd (tarea de transporte): copia el bloque de 192 bytes. Primero por el rango de bhi.w.
    A.label("hook_cmd")
    A.cmp_i(1, 0x20)
    A.bcond("eq", "hc_go")
    A.bw(CMD_UNKNOWN)
    A.label("hc_go")
    A.mov32(2, MBOX + MB_BLOCK)
    A.mov_r(3, 8)
    A.adds_imm8(3, 6)
    A.movs(4, 0)
    A.label("hc_loop")
    A.ldrb_r(5, 3, 4)
    A.strb_r(5, 2, 4)
    A.adds_imm8(4, 1)
    A.cmp_i(4, BLOCK_LEN)
    A.bcond("lo", "hc_loop")
    A.movs(5, 0)                         # terminadores fijos de cada texto
    A.mov_r(6, 2)
    prev = 0
    for end in TEXT_ENDS:
        A.adds_imm8(6, end - prev)
        A.strb_i(5, 6, 0)
        prev = end
    A.mov32(2, MBOX)
    A.ldr_i(0, 2, MB_SEQ)
    A.adds_imm8(0, 1)
    A.str_i(0, 2, MB_SEQ)
    A.mov32(0, MAGIC)
    A.str_i(0, 2, 0)
    A.bw(CMD_OK)

    # ---- hook_link: sin doble toque en la pagina 1 con indice 3
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
    A.bcond_w("eq", "ht_media_pg")
    A.cmp_i(3, 1)
    A.bcond_w("ne", "tap_orig")
    A.mov32(6, CFG_BYTE1)
    A.ldrb_i(6, 6, 0)
    A.and_imm8(6, 6, 3)
    A.cmp_i(6, 3)
    A.bcond_w("ne", "tap_orig")
    A.mov_r(6, 0)
    A.subs_imm8(6, PAGE_Y)
    A.cmp_i(6, 169)
    A.bcond_w("hi", "tap_orig")
    is_mixer(0, 1)                       # pisa r0/r1: para volver al original usar tap_orig_r
    A.bcond_w("eq", "ht_mix")
    # --- AUDIO
    A.cmp_i(6, 40)
    A.bcond("hs", "ha_lower")
    A.cmp_i(2, 34)
    A.bcond("lo", "ha_gallery")
    A.cmp_i(2, 66)
    A.bcond("lo", "ha_mixer")
    A.cmp_i(2, MIC_X - 6)
    A.bcond_w("lo", "tap_orig_r")
    A.imm(1, MIC_X + 30)
    A.cmp_r(2, 1)
    A.bcond_w("hs", "tap_orig_r")
    A.movs(1, C_MIC)
    A.bl("send_code")
    A.b("ht_ret")
    A.label("ha_gallery")
    A.movs(0, 2)
    A.bl("switch_to")
    A.b("ht_ret")
    A.label("ha_mixer")
    A.movs(0, 1)
    A.bl("switch_mode")
    A.movs(1, C_MIXER_OPEN)
    A.bl("send_code")
    A.b("ht_ret")
    A.label("ha_lower")
    A.movw(1, 319)
    A.cmp_r(2, 1)
    A.bcond_w("hi", "tap_orig_r")
    A.cmp_i(6, 128)
    A.bcond("hs", "ha_out")
    A.lsrs_i(6, 2, 6)
    A.addr(1, "codes")
    A.ldrb_r(1, 1, 6)
    A.bl("send_code")
    A.cmp_i(6, 2)
    A.bcond("ne", "ht_ret")
    A.bl("toggle_pp")
    A.b("ht_ret")
    A.label("ha_out")
    A.movs(1, C_OUTPUT)
    A.bl("send_code")
    A.b("ht_ret")
    # --- MEZCLADOR
    A.label("ht_mix")
    A.cmp_i(6, 40)
    A.bcond("hs", "hm_lower")
    A.cmp_i(2, 40)
    A.bcond_w("hs", "tap_orig_r")
    A.movs(0, 0)
    A.bl("switch_mode")
    A.b("ht_ret")
    A.label("hm_lower")
    A.cmp_i(6, 76)
    A.bcond("hs", "hm_vol")
    A.movs(1, C_APP_PREV)
    A.cmp_i(2, 110)
    A.bcond("lo", "hm_send")
    A.movs(1, C_APP_NEXT)
    A.cmp_i(2, 210)
    A.bcond("hs", "hm_send")
    A.movs(1, C_APP_MUTE)
    A.b("hm_send")
    A.label("hm_vol")
    A.movs(1, C_APP_DOWN)
    A.cmp_i(2, 160)
    A.bcond("lo", "hm_send")
    A.movs(1, C_APP_UP)
    A.label("hm_send")
    A.bl("send_code")
    A.b("ht_ret")
    # --- pagina Media (galeria): icono chico -> AUDIO
    A.label("ht_media_pg")
    A.mov_r(6, 0)
    A.subs_imm8(6, PAGE_Y)
    A.cmp_i(6, 40)
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

    # ---- rebuild_p1 / switch_mode / switch_to (igual que v4)
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

    A.label("switch_mode")               # r0 = 0 AUDIO / 1 MEZCLADOR
    A.push([4, "lr"], "push {r4, lr}")
    A.mov32(1, MBOX)
    A.movs(2, 0)
    A.cmp_i(0, 0)
    A.bcond("eq", "sm_set")
    A.mov32(2, MODE_MIXER)
    A.label("sm_set")
    A.str_i(2, 1, MB_MODE)
    A.bl("rebuild_p1")
    A.pop([4, "pc"], "pop {r4, pc}")

    A.label("switch_to")                 # r0 = indice 2 Media / 3 AUDIO
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
    A.bcond("ne", "rb_go")
    A.mov32(0, THUNK_MEDIA)
    A.b("rb_go")

    A.label("hook_map")
    A.cmp_i(0, 4)
    A.bcond("ne", "hm_orig")
    A.movs(1, 3)
    A.bw(0x08024A5E)
    A.label("hm_orig")
    A.raw("22fa01f1", "lsr.w r1, r2, r1")
    A.bw(0x08024A5E)

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

    A.label("media_ctor")
    A.push([4, "lr"], "push {r4, lr}")
    A.bl(F_MEDIA_CTOR)
    A.mov_r(4, 0)
    A.bl(F_SCREEN_ACTIVE)
    A.cmp_i(0, 0)
    A.bcond("eq", "mc_done")
    A.addr(1, "dsc_disc26")
    A.movs(2, 6)
    A.movs(3, PAGE_Y + 5)
    A.bl("mc_icon")
    A.label("mc_done")
    A.mov_r(0, 4)
    A.pop([4, "pc"], "pop {r4, pc}")
    # icono de la galeria -> AUDIO: disco oscuro + "play" en acento, colgados de la pantalla (r0)
    A.label("mc_icon")
    A.push([4, 5, 6, "lr"], "push {r4, r5, r6, lr}")
    A.mov_r(5, 0)
    A.bl("put_img")
    A.mov32(1, DARK)
    A.bl("recolor")
    A.mov_r(0, 5)
    A.addr(1, "dsc_to_audio")
    A.movs(2, 6)
    A.movs(3, PAGE_Y + 5)
    A.bl("put_img")
    A.mov32(1, ACCENT)
    A.bl("recolor")
    A.pop([4, 5, 6, "pc"], "pop {r4, r5, r6, pc}")

    # ---- helpers
    A.label("child")                     # r0 = hijo r1 de r0, o 0
    A.cmp_i(0, 0)
    A.bcond("eq", "ch_zero")
    A.ldr_i(2, 0, 8)
    A.cmp_i(2, 0)
    A.bcond("eq", "ch_zero")
    A.ldrh_i0(3, 2, 0x28)
    A.cmp_r(1, 3)
    A.bcond("hs", "ch_zero")
    A.ldr_i0(2, 2)
    A.lsls_i(1, 1, 2)
    A.add_r(1, 2)
    A.ldr_i0(0, 1)
    A.raw("7047", "bx lr")
    A.label("ch_zero")
    A.movs(0, 0)
    A.raw("7047", "bx lr")

    A.label("recolor")                   # (r0 obj, r1 0xRRGGBB); devuelve r0 = obj
    A.push([4, "lr"], "push {r4, lr}")
    A.mov_r(4, 0)
    A.cmp_i(0, 0)
    A.bcond("eq", "rc_done")
    A.mov_r(0, 1)
    A.bl(F_COLOR_HEX)
    A.raw("20f07f41", "bic r1, r0, #0xff000000")
    A.mov_r(0, 4)
    A.movs(2, 0)
    A.bl(F_STYLE_RECOLOR)
    A.mov_r(0, 4)
    A.movs(1, 0xFF)
    A.movs(2, 0)
    A.bl(F_STYLE_RECOLOR_OPA)
    A.label("rc_done")
    A.mov_r(0, 4)
    A.pop([4, "pc"], "pop {r4, pc}")

    A.label("put_img")                   # (r0 padre, r1 dsc, r2 x, r3 y) -> r0 imagen
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
    A.movs(1, 0x12)
    A.bl(F_REMOVE_FLAG)
    A.mov_r(0, 7)
    A.label("pi_done")
    A.add_sp(4)
    A.pop([4, 5, 6, 7, "pc"], "pop {r4, r5, r6, r7, pc}")

    A.label("mk_box")                    # (r0 padre, r1 x, r2 y, r3 = w | h<<16) -> r0 contenedor que recorta
    A.push([4, 5, 6, 7, "lr"], "push {r4, r5, r6, r7, lr}")
    A.sub_sp(4)
    A.mov_r(5, 1)
    A.mov_r(6, 2)
    A.mov_r(7, 3)
    A.bl(F_OBJ_CREATE)
    A.cmp_i(0, 0)
    A.bcond("eq", "mb_done")
    A.mov_r(4, 0)
    A.bl(F_REMOVE_STYLE_ALL)
    A.mov_r(0, 4)
    A.mov_r(1, 5)
    A.mov_r(2, 6)
    A.bl(F_SET_POS)
    A.mov_r(0, 4)
    A.uxth(1, 7)
    A.lsrs_i(2, 7, 16)
    A.bl(F_SET_SIZE)
    A.mov_r(0, 4)
    A.movs(1, 0x12)
    A.bl(F_REMOVE_FLAG)
    A.mov_r(0, 4)
    A.label("mb_done")
    A.add_sp(4)
    A.pop([4, 5, 6, 7, "pc"], "pop {r4, r5, r6, r7, pc}")

    def img(parent_reg, dsc, x, y, color):
        A.mov_r(0, parent_reg)
        A.addr(1, dsc)
        A.imm(2, x)
        A.imm(3, y)
        A.bl("put_img")
        if color is not None:
            A.mov32(1, color)
            A.bl("recolor")

    def label(parent_reg, text_label, x, y, w, h):
        A.label_call(parent_reg, text_label, FONT18, x, y, w, h)

    # ---- add_panel(r0 = pantalla)
    A.label("add_panel")
    A.push([4, 5, 6, "lr"], "push {r4, r5, r6, lr}")
    A.sub_sp(0x18)
    A.cmp_i(0, 0)
    A.bcond_w("eq", "ap_done")
    A.mov_r(6, 0)
    A.mov32(4, MBOX)                     # guard de armado
    A.ldrb_i(0, 4, MB_BUILD)
    A.cmp_i(0, 0)
    A.bcond("eq", "ap_guard_ok")
    A.movs(0, 0)
    A.str_i(0, 4, MB_MODE)
    A.label("ap_guard_ok")
    A.movs(0, 1)
    A.strb_i(0, 4, MB_BUILD)
    A.mov_r(0, 6)
    A.movs(1, 0)
    A.movs(2, PAGE_Y)
    A.movw(3, 320)
    A.movt(3, 170)
    A.bl("mk_box")                       # panel
    A.cmp_i(0, 0)
    A.bcond_w("eq", "ap_end")
    A.mov_r(5, 0)
    img(5, "dsc_panel_bg", 0, 0, None)   # 0: fondo (marca del panel)
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
    is_mixer(0, 1)
    A.bcond_w("eq", "ap_mix")
    # --- AUDIO
    label(5, "t_audio", 0, 7, 320, 20)                         # 3
    label(5, "t_empty", 6, 37, 308, 20)                        # 4: cancion
    A.mov_r(0, 5)
    A.movs(1, A_SONG)
    A.bl("child")
    A.cmp_i(0, 0)
    A.bcond("eq", "ap_no_song")
    A.movs(1, 3)                                               # LV_LABEL_LONG_SCROLL_CIRCULAR
    A.bl(F_LABEL_LONG_MODE)
    A.label("ap_no_song")
    A.mov_r(0, 5)                                              # 5: pildora
    A.movs(1, 10)
    A.movs(2, PILL_Y)
    A.movw(3, PILL_W)
    A.movt(3, PILL_H)
    A.bl("mk_box")
    A.mov_r(4, 0)
    img(4, "dsc_pill", 0, 0, DARK)
    img(4, "dsc_cap", PILL_W - CAP_W, 0, DARK)
    label(5, "t_empty", 0, PILL_Y + 3, 320, 20)               # 6: salida
    for x, glyph in ((6, "dsc_gallery"), (36, "dsc_mixer"), (MIC_X, "dsc_mic")):   # 7..12
        img(5, "dsc_disc26", x, 5, DARK)
        img(5, glyph, x, 5, ACCENT)
    for name, size, x, y in BUTTONS:                           # 13..17
        img(5, f"dsc_{name}", x, y, ACCENT)
    A.b("ap_common_tramp")
    # --- MEZCLADOR
    A.label("ap_mix")
    label(5, "t_mixer", 0, 7, 320, 20)                         # 3
    label(5, "t_empty", 0, 38, 320, 20)                        # 4: fila de la app
    img(5, "dsc_app0", 0, APP_ICON_Y, ACCENT)                  # 5: logo
    for i in range(MAX_DOTS):                                  # 6..11: puntitos (ocultos)
        img(5, "dsc_dot", 0, DOT_Y, GRAY)
        A.movs(1, 1)
        A.bl(F_ADD_FLAG)
    A.mov_r(0, 5)                                              # 12: barra
    A.movs(1, BAR_X)
    A.movs(2, BAR_Y)
    A.movs(3, BAR_W)
    A.movt(3, BAR_H)
    A.bl("mk_box")
    A.mov_r(4, 0)
    img(4, "dsc_bar", 0, 0, DARK)
    A.mov_r(0, 4)
    A.movs(1, 0)
    A.movs(2, 0)
    A.movs(3, 0)
    A.movt(3, BAR_H)
    A.bl("mk_box")                                             # relleno (ancho 0)
    A.mov_r(4, 0)
    img(4, "dsc_bar", 0, 0, ACCENT)
    label(5, "t_empty", BAR_X, 84, BAR_W, 20)                  # 13: porcentaje
    img(5, "dsc_disc26", 6, 5, DARK)                           # 14-15: volver
    img(5, "dsc_back", 6, 5, ACCENT)
    img(5, "dsc_minus", 10, 84, ACCENT)                        # 16-17
    img(5, "dsc_plus", 320 - 10 - BIG, 84, ACCENT)
    A.label("ap_common_tramp")
    A.movs(0, 35)                        # 18: % de bateria (fuente 11, alineado a la derecha)
    A.str_sp(0, 8)
    A.movs(0, 14)
    A.str_sp(0, 12)
    A.movs(0, 3)
    A.str_sp(0, 16)
    A.movw(0, 262)
    A.str_sp(0, 0)
    A.movs(0, 12)
    A.str_sp(0, 4)
    A.mov_r(0, 5)
    A.addr(1, "t_empty")
    A.mov32(2, FONT11)
    A.mov32(3, TEXT_COLOR)
    A.bl(F_LABEL)
    A.mov32(0, MBOX + MB_CACHE)          # la cancion se vuelve a poner en el label nuevo
    A.movs(1, 0xFF)
    A.strb_i(1, 0, 0)
    A.mov32(0, MBOX)                     # que my_tick reaplique todo
    A.ldr_i(1, 0, MB_SEQ)
    A.subs_imm8(1, 1)
    A.str_i(1, 0, MB_APPLIED)
    A.bl(F_STATUS_UPDATE)
    A.label("ap_end")
    A.mov32(0, MBOX)
    A.movs(1, 0)
    A.strb_i(1, 0, MB_BUILD)
    A.label("ap_done")
    A.add_sp(0x18)
    A.pop([4, 5, 6, "pc"], "pop {r4, r5, r6, pc}")

    # ---- find_panel() -> r0 panel (o 0)
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
    A.ldrh_i0(2, 1, 0x28)
    A.ldr_i0(1, 1)
    A.label("fp_loop")
    A.subs_imm8(2, 1)
    A.bcond("mi", "fp_none")
    A.lsls_i(3, 2, 2)
    A.add_r(3, 1)
    A.ldr_i0(3, 3)
    A.ldr_i(4, 3, 8)
    A.cmp_i(4, 0)
    A.bcond("eq", "fp_loop")
    A.ldrh_i0(6, 4, 0x28)
    A.cmp_i(6, 1)
    A.bcond("lo", "fp_loop")
    A.ldr_i0(4, 4)
    A.ldr_i0(7, 4)
    A.ldr_i(7, 7, 0x2C)
    A.cmp_r(7, 5)
    A.bcond("ne", "fp_loop")
    A.mov_r(0, 3)
    A.b("fp_ret")
    A.label("fp_none")
    A.movs(0, 0)
    A.label("fp_ret")
    A.add_sp(4)
    A.pop([4, 5, 6, 7, "pc"], "pop {r4, r5, r6, r7, pc}")

    # ---- toggle_pp: invierte play/pausa en AUDIO
    A.label("toggle_pp")
    A.push([4, "lr"], "push {r4, lr}")
    A.bl("find_panel")
    A.movs(1, A_PP)
    A.bl("child")
    A.cmp_i(0, 0)
    A.bcond("eq", "tp_done")
    A.mov_r(4, 0)
    A.ldr_i(0, 4, 0x2C)
    A.addr(1, "dsc_play")
    A.cmp_r(0, 1)
    A.bcond("eq", "tp_to_pause")
    A.addr(2, "dsc_pause")
    A.cmp_r(0, 2)
    A.bcond("ne", "tp_done")
    A.b("tp_set")
    A.label("tp_to_pause")
    A.addr(1, "dsc_pause")
    A.label("tp_set")
    A.mov_r(0, 4)
    A.bl(F_IMG_SET_SRC)
    A.label("tp_done")
    A.pop([4, "pc"], "pop {r4, pc}")

    # ---- set_child_text(r0 panel, r1 indice, r2 texto)
    A.label("set_child_text")
    A.push([4, "lr"], "push {r4, lr}")
    A.mov_r(4, 2)
    A.bl("child")
    A.cmp_i(0, 0)
    A.bcond("eq", "sct_done")
    A.mov_r(1, 4)
    A.bl(F_LABEL_SET_TEXT)
    A.label("sct_done")
    A.pop([4, "pc"], "pop {r4, pc}")

    # ---- set_song(r0 panel): copia y pone la cancion solo si difiere de la cache
    A.label("set_song")
    A.push([4, 5, 6, "lr"], "push {r4, r5, r6, lr}")
    A.mov_r(6, 0)
    A.mov32(4, MBOX + MB_BLOCK + B_SONG)
    A.mov32(5, MBOX + MB_CACHE)
    A.movs(3, 0)
    A.label("ss_cmp")
    A.ldrb_r(1, 4, 3)
    A.ldrb_r(2, 5, 3)
    A.cmp_r(1, 2)
    A.bcond("ne", "ss_diff")
    A.cmp_i(1, 0)
    A.bcond("eq", "ss_done")
    A.adds_imm8(3, 1)
    A.cmp_i(3, B_OUT - B_SONG)
    A.bcond("lo", "ss_cmp")
    A.b("ss_done")
    A.label("ss_diff")
    A.movs(3, 0)
    A.label("ss_copy")
    A.ldrb_r(1, 4, 3)
    A.strb_r(1, 5, 3)
    A.adds_imm8(3, 1)
    A.cmp_i(3, B_OUT - B_SONG)
    A.bcond("lo", "ss_copy")
    A.mov_r(0, 6)
    A.movs(1, A_SONG)
    A.mov_r(2, 4)
    A.bl("set_child_text")
    A.label("ss_done")
    A.pop([4, 5, 6, "pc"], "pop {r4, r5, r6, pc}")

    # ---- hook_sleep: en Media Control el flag "mouse dormido" se lee como 0
    A.label("hook_sleep")
    A.raw("9df84900", "ldrb.w r0, [sp, #0x49]")
    A.mov32(2, CFG_BYTE1)
    A.ldrb_i(2, 2, 0)
    A.and_imm8(2, 2, 3)
    A.cmp_i(2, 3)
    A.bcond("ne", "hsl_back")
    A.movs(0, 0)
    A.label("hsl_back")
    A.bw(SLEEP_SITE + 4)

    # ---- apply_audio(r0 panel); r7 = bloque
    A.label("apply_audio")
    A.push([4, 5, 6, "lr"], "push {r4, r5, r6, lr}")
    A.mov_r(5, 0)
    A.mov32(6, MBOX + MB_BLOCK)
    A.bl("set_song")                     # cancion (solo si cambio: no reinicia el deslizamiento)
    A.mov_r(0, 5)                        # bateria
    A.movs(1, I_BATT)
    A.mov_r(2, 6)
    A.adds_imm8(2, B_BATT)
    A.bl("set_child_text")
    A.mov_r(0, 5)                        # salida
    A.movs(1, A_OUT)
    A.mov_r(2, 6)
    A.adds_imm8(2, B_OUT)
    A.bl("set_child_text")
    A.mov_r(0, 5)                        # pildora: ancho y posicion
    A.movs(1, A_PILL)
    A.bl("child")
    A.cmp_i(0, 0)
    A.bcond("eq", "aa_mic")
    A.mov_r(4, 0)
    A.ldrh_i0(1, 6, B_PILLW)
    A.movs(2, PILL_H)
    A.bl(F_SET_SIZE)
    A.ldrh_i0(0, 6, B_PILLW)
    A.lsrs_i(0, 0, 1)
    A.movs(1, 160)
    A.subs_rrr(1, 1, 0)
    A.mov_r(0, 4)
    A.movs(2, PILL_Y)
    A.bl(F_SET_POS)
    A.mov_r(0, 4)                        # tapa derecha en w-12
    A.movs(1, 1)
    A.bl("child")
    A.cmp_i(0, 0)
    A.bcond("eq", "aa_mic")
    A.ldrh_i0(1, 6, B_PILLW)
    A.subs_imm8(1, CAP_W)
    A.movs(2, 0)
    A.bl(F_SET_POS)
    A.label("aa_mic")
    A.mov_r(0, 5)
    A.movs(1, A_MICDISC)
    A.bl("child")
    A.mov_r(4, 0)
    A.ldrb_i0(0, 6, B_MIC)
    A.cmp_i(0, 2)
    A.bcond("eq", "aa_muted")
    A.mov_r(0, 4)
    A.mov32(1, DARK)
    A.bl("recolor")
    A.mov_r(0, 5)
    A.movs(1, A_MIC)
    A.bl("child")
    A.mov_r(4, 0)
    A.addr(1, "dsc_mic")
    A.bl("aa_icon")
    A.mov_r(0, 4)
    A.mov32(1, ACCENT)
    A.bl("recolor")
    A.b("aa_play")
    A.label("aa_muted")
    A.mov_r(0, 4)
    A.mov32(1, RED)
    A.bl("recolor")
    A.mov_r(0, 5)
    A.movs(1, A_MIC)
    A.bl("child")
    A.mov_r(4, 0)
    A.addr(1, "dsc_mic_off")
    A.bl("aa_icon")
    A.mov_r(0, 4)
    A.mov32(1, WHITE)
    A.bl("recolor")
    A.label("aa_play")
    A.ldrb_i0(0, 6, B_PLAY)
    A.cmp_i(0, 1)
    A.bcond("ne", "aa_st2")
    A.addr(4, "dsc_pause")
    A.b("aa_setpp")
    A.label("aa_st2")
    A.cmp_i(0, 2)
    A.bcond("ne", "aa_done")
    A.addr(4, "dsc_play")
    A.label("aa_setpp")
    A.mov_r(0, 5)
    A.movs(1, A_PP)
    A.bl("child")
    A.mov_r(1, 4)
    A.bl("aa_icon")
    A.label("aa_done")
    A.pop([4, 5, 6, "pc"], "pop {r4, r5, r6, pc}")
    A.label("aa_icon")                   # set_src si el objeto existe (r0 obj, r1 dsc)
    A.cmp_i(0, 0)
    A.bcond("eq", "ai_ret")
    A.bw(F_IMG_SET_SRC)
    A.label("ai_ret")
    A.raw("7047", "bx lr")

    # ---- apply_mixer(r0 panel)
    A.label("apply_mixer")
    A.push([4, 5, 6, 7, "lr"], "push {r4, r5, r6, r7, lr}")
    A.sub_sp(4)
    A.mov_r(5, 0)
    A.mov32(6, MBOX + MB_BLOCK)
    A.movs(1, M_APP)                     # fila de la app
    A.mov_r(2, 6)
    A.adds_imm8(2, B_APP)
    A.bl("set_child_text")
    A.mov_r(0, 5)                        # bateria
    A.movs(1, I_BATT)
    A.mov_r(2, 6)
    A.adds_imm8(2, B_BATT)
    A.bl("set_child_text")
    A.mov_r(0, 5)                        # porcentaje
    A.movs(1, M_PCT)
    A.mov_r(2, 6)
    A.adds_imm8(2, B_PCT)
    A.bl("set_child_text")
    A.mov_r(0, 5)                        # logo de la app
    A.movs(1, M_ICON)
    A.bl("child")
    A.cmp_i(0, 0)
    A.bcond("eq", "am_dots")
    A.mov_r(4, 0)
    A.ldrb_i0(0, 6, B_APPICON)
    A.cmp_i(0, 4)
    A.bcond("ls", "am_icon_ok")
    A.movs(0, 0)
    A.label("am_icon_ok")
    A.lsls_i(0, 0, 2)
    A.addr(1, "app_icons")
    A.add_r(1, 0)
    A.ldr_i0(1, 1)
    A.mov_r(0, 4)
    A.bl(F_IMG_SET_SRC)
    A.mov_r(0, 4)
    A.ldrh_i0(1, 6, B_APPX)
    A.movs(2, APP_ICON_Y)
    A.bl(F_SET_POS)
    A.label("am_dots")                   # puntitos: r7 = i, [sp] = x
    A.movs(7, 0)
    A.ldrh_i0(0, 6, B_DOTX)
    A.str_sp(0, 0)
    A.label("am_dot_loop")
    A.mov_r(0, 5)
    A.adds_rri3(1, 7, 0)
    A.adds_imm8(1, M_DOT0)
    A.bl("child")
    A.cmp_i(0, 0)
    A.bcond("eq", "am_next")
    A.mov_r(4, 0)
    A.ldrb_i0(0, 6, B_DOTS)
    A.cmp_r(7, 0)
    A.bcond("lo", "am_show")
    A.mov_r(0, 4)
    A.movs(1, 1)
    A.bl(F_ADD_FLAG)
    A.b("am_next")
    A.label("am_show")
    A.mov_r(0, 4)
    A.movs(1, 1)
    A.bl(F_REMOVE_FLAG)
    A.mov_r(0, 4)
    A.raw("0099", "ldr r1, [sp]")
    A.movs(2, DOT_Y)
    A.bl(F_SET_POS)
    A.mov32(1, GRAY)
    A.ldrb_i0(0, 6, B_DOTIDX)
    A.cmp_r(7, 0)
    A.bcond("ne", "am_dotcol")
    A.mov32(1, ACCENT)
    A.label("am_dotcol")
    A.mov_r(0, 4)
    A.bl("recolor")
    A.label("am_next")
    A.raw("0098", "ldr r0, [sp]")
    A.adds_imm8(0, DOT_STEP)
    A.str_sp(0, 0)
    A.adds_imm8(7, 1)
    A.cmp_i(7, MAX_DOTS)
    A.bcond("lo", "am_dot_loop")
    A.mov_r(0, 5)                        # barra: ancho del relleno
    A.movs(1, M_BAR)
    A.bl("child")
    A.movs(1, 1)
    A.bl("child")
    A.cmp_i(0, 0)
    A.bcond("eq", "am_done")
    A.ldrh_i0(1, 6, B_BARW)
    A.movs(2, BAR_H)
    A.bl(F_SET_SIZE)
    A.label("am_done")
    A.add_sp(4)
    A.pop([4, 5, 6, 7, "pc"], "pop {r4, r5, r6, r7, pc}")

    # ---- my_tick (tarea LVGL): llamada original + aplicar el buzon
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
    A.mov_r(6, 0)
    is_mixer(0, 1)
    A.mov_r(0, 6)
    A.bcond("eq", "mt_mix")
    A.bl("apply_audio")
    A.b("mt_check")
    A.label("mt_mix")
    A.bl("apply_mixer")
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

    def place(name, blob):
        nonlocal p
        p = (p + 3) & ~3
        layout[name] = (p, blob)
        p += len(blob)

    place("t_audio", b"AUDIO\x00")
    place("t_mixer", MIXER_TITLE + b"\x00")
    place("t_empty", b"\x00")
    place("codes", bytes(CODES))
    place("dsc_panel_bg", v4.base_mem_dsc(MENU_BG_DSC))
    icons = build_icons()
    icons["to_audio"] = (SMALL, SMALL, render(lambda u, v: v2.glyph("play", u, v), SMALL))
    for name in icons:
        place(f"dsc_{name}", b"\x00" * 24)
        place(f"data_{name}", icons[name][2])
    place("app_icons", b"\x00" * 20)
    for name, (w, h, data) in icons.items():
        layout[f"dsc_{name}"] = (layout[f"dsc_{name}"][0],
                                 struct.pack("<BBHHHHHIII", 0x19, 0x0E, 0, w, h, w, 0, w * h, layout[f"data_{name}"][0], 0))
    layout["app_icons"] = (layout["app_icons"][0], struct.pack("<5I", *(layout[f"dsc_app{i}"][0] for i in range(5))))
    for name, (addr, _) in layout.items():
        A.labels[name] = addr

    code, listing = A.assemble()
    verify(listing)
    blob = bytearray(code)
    for name, (addr, b) in sorted(layout.items(), key=lambda kv: kv[1][0]):
        blob += b"\x00" * (addr - (CODE + len(blob)))
        blob += b
    return A, bytes(blob), listing, icons


MIXER_TITLE = b"MEZCLADOR"   # --english: b"MIXER" (release publico)


def hex_records(start, blob):
    """registros Intel HEX (16 bytes) con un registro tipo 04 cada vez que cambia el segmento de 64 KB."""
    out, upper = [], start >> 16
    for i in range(0, len(blob), 16):
        a = start + i
        if (a >> 16) != upper:
            upper = a >> 16
            b = bytes([2, 0, 0, 4, upper >> 8, upper & 0xFF])
            out.append(":" + (b + bytes([(-sum(b)) & 0xFF])).hex().upper() + "\r\n")
        chunk = blob[i:i + 16]
        assert (a & 0xFFFF) + len(chunk) <= 0x10000
        b = bytes([len(chunk), (a >> 8) & 0xFF, a & 0xFF, 0]) + chunk
        out.append(":" + (b + bytes([(-sum(b)) & 0xFF])).hex().upper() + "\r\n")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--persist", action="store_true")
    ap.add_argument("--english", action="store_true", help='titulo "MIXER" en vez de "MEZCLADOR"')
    args = ap.parse_args()
    global MIXER_TITLE
    if args.english:
        MIXER_TITLE = b"MIXER"
    out_dir = ROOT / ("patched-screen6" + ("-en" if args.english else "") + ("-persist" if args.persist else ""))
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
    v4._MEM.update(mem)
    orig = dict(mem)

    A, blob, listing, icons = build()
    print(f"codigo + datos: {CODE:#x}..{CODE + len(blob):#x} ({len(blob)} bytes)")
    for a, b, t in listing:
        print(f"  {a:08X}: {b.hex():8}  {t}")
    v4.SITES.clear()
    v4.SITES.update(SITES)
    v4.apply(mem, A, args.persist)

    for idx, start, b in recs:
        if any(mem[start + k] != b[4 + k] for k in range(b[0])):
            rb = bytearray(b)
            for k in range(b[0]):
                rb[4 + k] = mem[start + k]
            rb[-1] = (-sum(rb[:-1])) & 0xFF
            eol = lines[idx][len(lines[idx].rstrip("\r\n")):]
            lines[idx] = ":" + rb.hex().upper() + eol
    last_idx = recs[-1][0]
    lines[last_idx + 1:last_idx + 1] = hex_records(CODE, blob)
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
