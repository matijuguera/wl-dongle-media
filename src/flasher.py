"""Flashea el chip de pantalla (MH2435) del dongle Beast X V2 por USB, sin Chrome.

Es una copia fiel del camino "512" del driver web oficial (WL_X_V2: enterBL512, erase512, program512,
verify512, exitBL512 en index-uGTnF_eK.js), pero SIN reflashear el nRF (el chip USB): ese paso de
la web es opcional y es el unico que puede dejar el dongle sin USB.

    python flasher.py version                  # version actual del firmware de pantalla
    python flasher.py plan  <archivo.hex>      # arma los paquetes y muestra un resumen (no toca el dongle)
    python flasher.py flash <archivo.hex>      # flashea (cerrar antes dongle_media.py y la app de WLMouse)
"""
import hashlib
import math
import sys
import time

import hid

VID, PID = 0x36A7, 0xA856
LCD_IFACE = 3
TARGET = 3                    # id del MH2435 en el camino 512 de la web
CHUNK = 496                   # programDataSize de la web para WL_X_V2
B0, OK_CODES = 0xB0, (0xA1, 0x02)
CMD_ERASE, CMD_PROGRAM, CMD_VERIFY, CMD_BL_VER, CMD_EXIT = 0x01, 0x0D, 0x8E, 0x80, 0x04
RETRY_GETS = 30               # fwRetryGetCount


class FlashError(Exception):
    pass


# ------------------------------------------------------------------ hex -> imagen (igual que la web)
def load_hex(text):
    """Port literal de `Aye` (el lector de .hex de la web): devuelve (imagen, direccion base).
    Rellena los huecos entre registros con 00 igual que ella, para que la imagen sea identica."""
    nan = float("nan")

    def hexint(x):
        try:
            return int(x, 16)
        except ValueError:
            return nan

    ext, data, addrs = [], [], []
    h, m, f = 0, 0, ""
    for line in text.replace("\n", "").split("\r"):
        if line[7:9] == "04":
            ext.append(int(line[9:13], 16))
        else:
            u = hexint(line[3:7])
            if u == 0:
                h, f = 0, line
            if h == 0:
                h, f = u, line
            else:
                p = u - h
                if p != 16:
                    k = 0
                    while k < p - m:
                        data.append("00")
                        k += 1
                elif f[1:3] != "10":
                    data.extend(["00"] * (16 - int(f[1:3], 16)))
                h, f = u, line
        if line[:1] == ":":
            if line[1:9] == "00000001":
                continue
            if line[8:9] in ("0", "1"):
                data.append(line[9:len(line) - 2])
                m = (len(line) - 11) / 2
                addrs.append(int(line[3:7], 16))
    image = bytes.fromhex("".join(data))
    return image, addrs[0] + (ext[0] << 16)


def packets(image, base):
    """Paquetes de programa de 512 B (`Wve` + `programByDataParams512`): datos XOR 0x55."""
    out = []
    for off in range(0, len(image), CHUNK):
        chunk = image[off:off + CHUNK]
        n = len(chunk)
        a = bytearray(512)
        a[2], a[3], a[4], a[5] = TARGET, (n + 5) & 0xFF, B0, CMD_PROGRAM
        a[6], a[7] = n >> 8, n & 0xFF
        a[8:12] = (base + off).to_bytes(4, "big")
        a[12:12 + n] = bytes(b ^ 0x55 for b in chunk)
        out.append(bytes(a))
    return out


# ------------------------------------------------------------------ respuestas
def data_offset(r):
    return 0 if len(r) < 8 or r[4] == B0 else (1 if r[5] == B0 else 0)


def status_ok(r):
    if not r or len(r) < 2:
        return False
    o = data_offset(r)
    return r[1 + o] in OK_CODES or r[o] in OK_CODES


def is_ack(r, cmd):
    o = data_offset(r)
    return len(r) > 5 + o and r[4 + o] == B0 and r[5 + o] == cmd


def echo_ok(pkt, r):
    """La respuesta repite direccion y datos del paquete (`compareFwEcho`)."""
    n = pkt[6] << 8 | pkt[7]
    o = data_offset(r)
    if n <= 0 or len(r) < 12 + o + n:
        return False
    return bytes(r[8 + o:12 + o]) == pkt[8:12] and bytes(r[12 + o:12 + o + n]) == pkt[12:12 + n]


# ------------------------------------------------------------------ dispositivo
class Lcd:
    """Canal de 512 B (interfaz 3). Indices de buffer iguales a los de la web."""

    def __init__(self):
        self.dev = None

    def open(self, tries=1, wait=0.2):
        for i in range(tries):
            self.close()
            path = next((d["path"] for d in hid.enumerate(VID, PID) if d["interface_number"] == LCD_IFACE), None)
            if path:
                try:
                    self.dev = hid.device()
                    self.dev.open_path(path)
                    return True
                except OSError:
                    self.dev = None
            if i + 1 < tries:
                time.sleep(wait)
        return False

    def close(self):
        if self.dev:
            try:
                self.dev.close()
            except OSError:
                pass
        self.dev = None

    def send(self, buf):
        try:
            self.dev.send_feature_report(bytes([0]) + bytes(buf))
        except (OSError, AttributeError):
            pass

    def get(self):
        try:
            return self.dev.get_feature_report(0, 513)
        except (OSError, AttributeError):
            return []

    def wait_ack(self, buf, cmd, delay_s, check=None):
        """`waitFeature512CmdAck` / `retryFeature512Get`: espera y relee con pausas crecientes."""
        check = check or (lambda r: is_ack(r, cmd) and status_ok(r))
        self.send(buf)
        time.sleep(delay_s)
        r = self.get()
        for c in range(1, RETRY_GETS):
            if check(r):
                return r
            time.sleep(c / 1000)
            r = self.get()
        return r


def cmd_buf(*pairs):
    b = bytearray(512)
    b[2] = TARGET
    for i, v in pairs:
        b[i] = v
    return b


def app_version(lcd):
    """`getFirmwareVersionDeviceID512`: '0.0.15.0', o None."""
    lcd.send(cmd_buf((3, 16), (5, 0x81)))
    time.sleep(0.1)
    r = lcd.get()
    if len(r) > 10 and r[6] == 0x81:
        return ".".join(str(x) for x in r[7:11])
    if len(r) > 9 and r[5] == 0x81:
        return ".".join(str(x) for x in r[6:10])
    return None


def bl_version(lcd):
    """`getBLFWVer512`: version del bootloader (contiene 'B' cuando esta en modo actualizacion)."""
    lcd.send(cmd_buf((3, 6), (4, B0), (5, CMD_BL_VER)))
    time.sleep(0.1)
    r = lcd.get()
    for o in (0, 1):
        if len(r) > 12 - o and r[1 - o] in OK_CODES:
            return f"{r[9 - o]:x}." + ".".join(f"{x:02x}" for x in r[10 - o:13 - o])
    return "0.0.0.0"


def flash(image, base, log=print, progress=lambda stage, frac: None):
    """Flashea `image` en el MH2435. Lanza FlashError si algo falla antes de terminar."""
    pkts = packets(image, base)
    lcd = Lcd()
    if not lcd.open(tries=10):
        raise FlashError("dongle not found (screen interface)")
    try:
        log(f"current firmware: {app_version(lcd)}")

        # entrar al bootloader (enterBL512): 3 veces, sin leer respuesta
        enter = cmd_buf((3, 1), (6, B0))
        for _ in range(3):
            lcd.send(enter)
            time.sleep(0.1)
        time.sleep(0.5)
        if not lcd.open(tries=20):
            raise FlashError("the dongle did not come back after requesting update mode")
        ver = "0.0.0.0"
        for _ in range(50):
            ver = bl_version(lcd)
            if "B" in ver.upper():
                break
            time.sleep(0.5)
        if "B" not in ver.upper():
            raise FlashError("the screen did not enter update mode (nothing was erased)")
        log(f"update mode: {ver}")
        time.sleep(1.0)

        log("erasing...")
        lcd.wait_ack(cmd_buf((3, 8), (4, B0), (5, CMD_ERASE)), CMD_ERASE, 0.2)

        for i, pkt in enumerate(pkts):
            extra = 0.0
            for _ in range(5):
                r = lcd.wait_ack(pkt, CMD_PROGRAM, 0.001 + extra)
                if status_ok(r) and echo_ok(pkt, r):
                    break
                extra += 0.001
            else:
                raise FlashError(f"failed to program block {i + 1}/{len(pkts)}")
            progress("program", (i + 1) / len(pkts))

        readback = bytearray()
        for i, pkt in enumerate(pkts):
            n = pkt[6] << 8 | pkt[7]
            q = cmd_buf((3, n & 0xFF), (4, B0), (5, CMD_VERIFY), (6, n >> 8), (7, n & 0xFF))
            q[8:12] = pkt[8:12]
            ok = lambda r: is_ack(r, CMD_VERIFY) and status_ok(r) and echo_ok(pkt, r)
            extra = 0.0
            for _ in range(5):
                r = lcd.wait_ack(q, CMD_VERIFY, 0.001 + extra, check=ok)
                if ok(r):
                    break
                extra += 0.001
            else:
                raise FlashError(f"failed to verify block {i + 1}/{len(pkts)}")
            o = data_offset(r)
            readback += bytes(b ^ 0x55 for b in r[12 + o:12 + o + n])
            progress("verify", (i + 1) / len(pkts))
        if bytes(readback[:len(image)]) != image:
            raise FlashError("verification mismatch")
        log("Verify OK")

        lcd.send(cmd_buf((3, 1), (4, B0), (5, CMD_EXIT), (6, 1)))
        time.sleep(2.0)
        for _ in range(150):
            if lcd.open():
                ver = app_version(lcd)
                if ver and ver != "0.0.0.0":
                    log(f"done, firmware {ver}")
                    return ver
            time.sleep(0.5)
        raise FlashError("flashed and verified, but the screen did not answer after restarting")
    finally:
        lcd.close()


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("version", "plan", "flash"):
        sys.exit(__doc__)
    if sys.argv[1] == "version":
        lcd = Lcd()
        if not lcd.open():
            sys.exit("no encuentro el dongle")
        print(app_version(lcd))
        lcd.close()
        return
    text = open(sys.argv[2], "rb").read().decode("ascii")
    image, base = load_hex(text)
    pkts = packets(image, base)
    print(f"imagen {len(image)} B en 0x{base:08X}, sha256 {hashlib.sha256(image).hexdigest()[:16]}, "
          f"{len(pkts)} paquetes de {CHUNK} B")
    if sys.argv[1] == "flash":
        def bar(stage, frac):
            print(f"\r{stage} {math.floor(frac * 100):3d}%", end="", flush=True)
        try:
            flash(image, base, log=lambda m: print("\n" + m), progress=bar)
        except FlashError as e:
            sys.exit(f"\nERROR: {e}")


if __name__ == "__main__":
    main()
