"""Puente PC <-> dongle WLMouse Beast X V2 (pantallas AUDIO y MEZCLADOR, firmware v6).

Dongle -> PC: reportes vendor `04 CD <codigo>` por la interfaz HID 0xFFA0 (36A7:A856):
    0x01/0x02 doble toque, 0x10 play/pausa  -> Play/Pause
    0x11 siguiente, 0x12 anterior           -> Next / Previous track
    0x13 / 0x14 volumen general + / -       -> Volume up / down
    0x15 tocar la salida                    -> siguiente salida de audio activa
    0x18 / 0x19 mezclador - / +             -> volumen de la app elegida (2 %)
    0x1A microfono                          -> mutear/desmutear el microfono predeterminado
    0x1B / 0x1C flechas del mezclador       -> app anterior / siguiente
    0x1D se abrio el mezclador              -> volver a Spotify (o la primera app)
    0x1E tocar el nombre de la app          -> mutear/desmutear esa app
Bateria del mouse: consulta 0x83 por el canal de comandos de 64 B (interfaz 2) cada 30 s, y los avisos
`04 03 .. pct` que manda el dongle.
PC -> dongle: comando LCD 0x20 (feature report de 512 B, interfaz 3) con un bloque de 208 bytes ya
"renderizado": textos, estados y posiciones (medidas con la fuente real del dongle, font18.json).
Se manda cuando algo cambia y cada 15 s; se reintenta hasta que el dongle responde A1.

    python dongle_media.py          # queda escuchando (Ctrl+C para salir)
    pythonw dongle_media.py         # igual, sin ventana
    python dongle_media.py --check  # verifica que encuentra las interfaces y muestra el estado
"""
import ctypes
import json
import pathlib
import struct
import sys
import time
import traceback

import hid

import audio_out
import pc_state

VID, PID = 0x36A7, 0xA856
USAGE_PAGE = 0xFFA0
LCD_IFACE = 3
CMD_IFACE = 2                  # canal de comandos de 64 bytes (lo usa la app oficial)
BATT_EVERY = 30.0
REPORT_ID, MARKER = 0x04, 0xCD
CMD_MEDIA_INFO = 0x20
RESP_OK = 0xA1
KEYEVENTF_KEYUP = 0x2
RESEND_EVERY = 15.0
POLL_EVERY = 1.0

KEYS = {0x01: 0xB3, 0x02: 0xB3, 0x10: 0xB3, 0x11: 0xB0, 0x12: 0xB1, 0x13: 0xAF, 0x14: 0xAE}
C_OUTPUT, C_APP_DOWN, C_APP_UP, C_MIC, C_APP_PREV, C_APP_NEXT, C_MIXER_OPEN, C_APP_MUTE = \
    0x15, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E

# simbolos LVGL que trae la fuente del dongle
SYM_AUDIO, SYM_VOLUME, SYM_LEFT, SYM_RIGHT = "", "", "", ""
# geometria (igual que make_media_screen5.py)
SCREEN_W, PILL_MAX, PILL_PAD, BAR_W, APP_ICON, DOT_STEP, MAX_DOTS = 320, 300, 24, 172, 18, 11, 6
B_PLAY, B_MIC, B_APPICON, B_DOTS, B_DOTIDX, B_PILLW, B_APPX, B_DOTX, B_BARW = 0, 1, 2, 3, 4, 6, 8, 10, 12
B_SONG, B_OUT, B_APP, B_PCT, B_BATT, BLOCK_LEN = 16, 80, 128, 176, 192, 208

_FONT = json.loads(pathlib.Path(__file__).with_name("font18.json").read_text())
ADV = {int(k): v for k, v in _FONT["adv"].items()}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def press(vk):
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def code_of(report):
    if len(report) >= 3 and report[0] == REPORT_ID and report[1] == MARKER:
        return report[2]
    return None


def clean(text):
    """Solo caracteres que existen en la fuente del dongle; sin espacios ni guiones sueltos."""
    import re
    import unicodedata
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if ord(c) in ADV)          # ej. japones -> se descarta
    t = re.sub(r"[(\[]\s*((feat|ft|with)\.?)?\s*[)\]]", "", t, flags=re.I)   # parentesis que quedaron vacios
    t = re.sub(r"(\s*-\s*){2,}", " - ", t)                 # "a - - b" -> "a - b"
    t = " ".join(t.split())
    return t.strip(" -")


def width(text):
    return sum(ADV.get(ord(c), 0) for c in text)


def fit(text, max_bytes):
    """Recorta para que entre (UTF-8 + terminador) en el campo del bloque."""
    while len(text.encode("utf-8")) > max_bytes - 1:
        text = text[:-1]
    return text


# ------------------------------------------------------------------ estado de la PC -> bloque
def read_battery():
    """% de bateria del mouse (consulta de solo lectura que usa la app oficial), o None."""
    path = next((d["path"] for d in hid.enumerate(VID, PID)
                 if d["interface_number"] == CMD_IFACE and d["usage_page"] == 0xFFFF), None)
    if path is None:
        return None
    dev = hid.device()
    try:
        dev.open_path(path)
        q = bytearray(64)
        q[2], q[3], q[5] = 2, 2, 0x83
        dev.send_feature_report(bytes([0]) + bytes(q))
        time.sleep(0.1)
        r = dev.get_feature_report(0, 65)
        if len(r) > 8 and r[1] == 0xA1 and r[4] == 2 and r[6] == 0x83 and 0 < r[8] <= 100:
            return r[8]
        return None
    except OSError:
        return None
    finally:
        dev.close()


class State:
    def __init__(self):
        self.last_song = ""
        self.app = None          # exe elegido en el mezclador
        self.battery = None      # % del mouse

    def pick_app(self, apps, delta=0, reset=False):
        if not apps:
            self.app = None
            return
        if reset or self.app not in apps:
            self.app = "spotify.exe" if "spotify.exe" in apps else apps[0]
        if delta:
            self.app = apps[(apps.index(self.app) + delta) % len(apps)]

    def block(self):
        b = bytearray(BLOCK_LEN)
        # AUDIO: cancion y estado de Spotify (cerrado: nada; pausado: ultima cancion con el icono de play)
        status, title = pc_state.spotify_status()
        if title:
            self.last_song = clean(title)
        if status == "cerrado":
            self.last_song = ""
        b[B_PLAY] = 1 if status == "sonando" else 2
        if self.last_song:
            put_text(b, B_SONG, B_OUT - B_SONG, SYM_AUDIO + "  " + self.last_song)
        put_text(b, B_BATT, BLOCK_LEN - B_BATT, f"{self.battery}%" if self.battery else "")
        # microfono
        m = pc_state.mic_muted()
        b[B_MIC] = 0 if m is None else (2 if m else 1)
        # salida de audio + ancho de la pildora
        cur = audio_out.default_output_id()
        name = next((d["name"] for d in audio_out.list_outputs() if d["id"] == cur), "")
        out = fit(SYM_VOLUME + "  " + (clean(name) or "No output"), B_APP - B_OUT)
        put_text(b, B_OUT, B_APP - B_OUT, out)
        struct.pack_into("<H", b, B_PILLW, int(min(PILL_MAX, max(60, width(out) + PILL_PAD))))
        # MEZCLADOR
        apps = pc_state.list_apps()
        self.pick_app(apps)
        if self.app:
            disp, icon = pc_state.app_display(self.app)
            disp = clean(disp)
            gap = " " * max(1, round((APP_ICON + 6) / ADV[32]))
            row = fit(SYM_LEFT + "   " + gap + disp + "   " + SYM_RIGHT, B_PCT - B_APP)
            start = (SCREEN_W - width(row)) / 2
            icon_x = start + width(SYM_LEFT + "   ") + (width(gap) - APP_ICON) / 2
            vol, muted = pc_state.app_volume(self.app)
            vol = vol or 0
            pct = "Mute" if muted else f"{vol}%"
            idx = apps.index(self.app)
        else:
            row, icon, icon_x, vol, pct, idx = "No apps playing", 0, 400, 0, "", 0
        put_text(b, B_APP, B_PCT - B_APP, row)
        put_text(b, B_PCT, BLOCK_LEN - B_PCT, pct)
        b[B_APPICON] = icon
        struct.pack_into("<H", b, B_APPX, int(round(icon_x)))
        count = min(MAX_DOTS, len(apps))
        b[B_DOTS], b[B_DOTIDX] = count, min(idx, MAX_DOTS - 1)
        struct.pack_into("<H", b, B_DOTX, int(round(SCREEN_W / 2 - (count * DOT_STEP - 5) / 2)))
        struct.pack_into("<H", b, B_BARW, round(vol * BAR_W / 100))
        return bytes(b)


def put_text(buf, off, size, text):
    data = fit(text, size).encode("utf-8")
    buf[off:off + len(data)] = data


def send_block(block):
    path = next((d["path"] for d in hid.enumerate(VID, PID) if d["interface_number"] == LCD_IFACE), None)
    if path is None:
        return None
    pkt = bytearray(512)
    pkt[3], pkt[4], pkt[5] = BLOCK_LEN + 1, 5, CMD_MEDIA_INFO
    pkt[6:6 + BLOCK_LEN] = block
    dev = hid.device()
    try:
        dev.open_path(path)
        dev.send_feature_report(bytes([0]) + bytes(pkt))
        time.sleep(0.03)
        resp = dev.get_feature_report(0, 513)
        return resp[1] if len(resp) > 1 else None
    except OSError:
        return None
    finally:
        dev.close()


def handle(code, st):
    if code in KEYS:
        press(KEYS[code])
    elif code == C_OUTPUT:
        nxt = audio_out.next_output()
        log(f"salida -> {nxt['name'] if nxt else 'ninguna'}")
    elif code == C_MIC:
        log(f"microfono muteado: {pc_state.toggle_mic()}")
    elif code in (C_APP_PREV, C_APP_NEXT, C_MIXER_OPEN):
        st.pick_app(pc_state.list_apps(), delta={C_APP_PREV: -1, C_APP_NEXT: 1}.get(code, 0), reset=code == C_MIXER_OPEN)
        log(f"mezclador -> {st.app}")
    elif code in (C_APP_DOWN, C_APP_UP) and st.app:
        log(f"{st.app} -> {pc_state.change_app_volume(st.app, -1 if code == C_APP_DOWN else 1)}%")
    elif code == C_APP_MUTE and st.app:
        log(f"{st.app} muteada: {pc_state.toggle_app_mute(st.app)}")


def listen(active=lambda: True):
    """Bucle del puente. `active()` en False lo pausa (suelta el dongle), p. ej. mientras se flashea."""
    st = State()
    while True:
        if not active():
            time.sleep(0.5)
            continue
        path = next((d["path"] for d in hid.enumerate(VID, PID) if d["usage_page"] == USAGE_PAGE), None)
        if path is None:
            time.sleep(3)
            continue
        dev = hid.device()
        try:
            dev.open_path(path)
            log("conectado al dongle")
            last_block, last_sent, next_poll, next_batt = None, 0.0, 0.0, 0.0
            while active():
                report = dev.read(64, 250)
                if report and len(report) > 4 and report[0] == REPORT_ID and report[1] == 0x03 and 0 < report[3] <= 100:
                    st.battery = report[3]             # aviso de bateria del dongle
                if time.monotonic() >= next_batt:
                    next_batt = time.monotonic() + BATT_EVERY
                    st.battery = read_battery() or st.battery
                code = code_of(report) if report else None
                if code is not None:
                    handle(code, st)
                    next_poll = 0.0
                now = time.monotonic()
                if now >= next_poll:
                    next_poll = now + POLL_EVERY
                    blk = st.block()
                    if blk != last_block or now - last_sent >= RESEND_EVERY:
                        r = send_block(blk)
                        if r == RESP_OK:
                            last_block, last_sent = blk, now
                        else:
                            last_block = None  # la pantalla todavia no esta lista: reintentar
        except OSError:
            log("dongle desconectado, reintentando")
        except Exception:  # noqa: BLE001 - un error raro (audio, COM...) no tiene que matar el puente
            log("error en el puente, reintentando:\n" + traceback.format_exc())
        finally:
            dev.close()
        time.sleep(2)


if __name__ == "__main__":
    if "--check" in sys.argv:
        ok = all(any(p(d) for d in hid.enumerate(VID, PID)) for p in
                 (lambda d: d["usage_page"] == USAGE_PAGE, lambda d: d["interface_number"] == LCD_IFACE))
        print("interfaces del dongle:", "OK" if ok else "NO encontradas")
        blk = State().block()
        print("bateria:", read_battery())
        for name, off, end in (("cancion", B_SONG, B_OUT), ("salida", B_OUT, B_APP), ("app", B_APP, B_PCT), ("pct", B_PCT, B_BATT)):
            print(f"  {name:8}", repr(blk[off:end].split(b"\0")[0].decode("utf-8")))
        print("  play/mic/icono/puntos/idx:", list(blk[:5]), "pildora/iconoX/puntosX/barra:", struct.unpack_from("<4H", blk, 6))
        sys.exit(0 if ok else 1)
    try:
        listen()
    except KeyboardInterrupt:
        pass
