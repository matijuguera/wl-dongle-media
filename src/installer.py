"""Instala (o saca) la pantalla Media Control en el dongle Beast X V2.

  1. Baja el firmware de pantalla OFICIAL del servidor de WLMouse (o usa un .hex local con --hex).
  2. Comprueba su SHA-256: solo se parchea la version exacta para la que se hizo el parche.
  3. Le aplica `patch_v6.json` (nuestros bytes) y comprueba el resultado.
  4. Lo flashea (flasher.py: entra al bootloader, borra, programa, verifica byte por byte, reinicia).
  5. Deja "Media Control" como pantalla por defecto.

    python installer.py                 # instala
    python installer.py --restore       # vuelve al firmware oficial
    python installer.py --status        # que tiene instalado el dongle (no escribe nada)
"""
import hashlib
import json
import pathlib
import sys
import time
import urllib.request

import flasher

PATCH = json.loads(pathlib.Path(__file__).with_name("patch_v6.json").read_text(encoding="utf-8"))
DEFAULT_MEDIA_CONTROL = 4
CMD_MEDIA_INFO = 0x20


class InstallError(Exception):
    pass


def sha(b):
    return hashlib.sha256(b).hexdigest()


def official_image(hex_path=None, log=print):
    """(imagen, base) del firmware oficial, verificada."""
    if hex_path:
        raw = pathlib.Path(hex_path).read_bytes()
    else:
        log("downloading the official WLMouse firmware...")
        req = urllib.request.Request(PATCH["official_url"], headers={"User-Agent": "wlmouse-dongle-mod"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
        except OSError as e:
            raise InstallError(f"could not download the official firmware ({e})") from e
    if sha(raw) != PATCH["official_hex_sha256"]:
        raise InstallError("the official firmware is not the expected version (v0.0.15.0); nothing was changed")
    image, base = flasher.load_hex(raw.decode("ascii"))
    if sha(image) != PATCH["official_image_sha256"] or base != PATCH["base"]:
        raise InstallError("the official image does not match; nothing was changed")
    return image, base


def patched_image(image):
    out = bytearray(image) + bytes(max(0, PATCH["size"] - len(image)))
    for off, data in PATCH["runs"]:
        b = bytes.fromhex(data)
        out[off:off + len(b)] = b
    out = bytes(out[:PATCH["size"]])
    if sha(out) != PATCH["result_sha256"]:
        raise InstallError("the patch did not produce the expected image; nothing was changed")
    return out


def lcd_command(lcd, cmd, payload=b""):
    """Comando LCD (grupo 5) por el canal de 512 B. Devuelve el codigo de respuesta (A1 = OK)."""
    b = bytearray(512)
    b[3], b[4], b[5] = len(payload) + 1, 5, cmd
    b[6:6 + len(payload)] = payload
    for _ in range(5):
        lcd.send(b)
        time.sleep(0.03)
        r = lcd.get()
        if len(r) > 1 and r[1] in (0xA1, 0xA3):
            return r[1]
        time.sleep(0.1)
    return None


def status():
    """('oficial'|'media-control'|'update-mode'|None, version). None = no se encontro el dongle.
    'update-mode' = la pantalla quedo en el bootloader (p. ej. un flasheo cortado): se puede reinstalar."""
    lcd = flasher.Lcd()
    if not lcd.open():
        return None, None
    try:
        ver = None
        for _ in range(5):                  # a veces la primera lectura llega vacia (0.0.0.0)
            ver = flasher.app_version(lcd)
            if ver and ver != "0.0.0.0":
                break
            time.sleep(0.3)
        if ver in (None, "0.0.0.0") and "B" in flasher.bl_version(lcd).upper():
            return "update-mode", None
        # el firmware oficial no conoce el comando 0x20 (responde A3); el nuestro responde A1
        code = lcd_command(lcd, CMD_MEDIA_INFO)
        return ("media-control" if code == 0xA1 else "oficial"), ver
    finally:
        lcd.close()


def set_default_screen(value=DEFAULT_MEDIA_CONTROL):
    lcd = flasher.Lcd()
    if not lcd.open(tries=10):
        raise InstallError("dongle not found to set the default screen")
    try:
        if lcd_command(lcd, 0x0A, bytes([value])) != 0xA1:
            raise InstallError("the dongle did not accept the default screen")
    finally:
        lcd.close()


def install(restore=False, hex_path=None, log=print, progress=lambda stage, frac: None):
    kind, ver = status()
    if kind is None:
        raise InstallError("dongle not found: plug it in and close the WLMouse app")
    if kind != "update-mode" and ver != "0.0.15.0":
        raise InstallError(f"the dongle has firmware {ver}; this patch only supports 0.0.15.0")
    image, base = official_image(hex_path, log)
    target = image if restore else patched_image(image)
    try:
        flasher.flash(target, base, log=log, progress=progress)
    except flasher.FlashError as e:
        raise InstallError(str(e)) from e
    time.sleep(1.0)
    set_default_screen(1 if restore else DEFAULT_MEDIA_CONTROL)
    log("done" + ("" if restore else ": Media Control is now the default screen"))


def main():
    if "--status" in sys.argv:
        kind, ver = status()
        print("dongle not found" if kind is None else f"firmware {ver}, screen: {kind}")
        return
    hex_path = sys.argv[sys.argv.index("--hex") + 1] if "--hex" in sys.argv else None

    def bar(stage, frac):
        print(f"\r{'programming' if stage == 'program' else 'verifying'} {int(frac * 100):3d}%", end="", flush=True)

    try:
        install(restore="--restore" in sys.argv, hex_path=hex_path, log=lambda m: print("\n" + m), progress=bar)
    except InstallError as e:
        sys.exit(f"\nERROR: {e}")


if __name__ == "__main__":
    main()
