"""Genera el firmware MH2435 parcheado: doble toque -> aviso a la PC (en vez de abrir la web).

Cambio unico: en 0x0801A67C (builder del mensaje del doble toque)
    movw r2, #0x0103   ->  movw r2, #0xCD04
El mensaje pasa de [03 01 modo ...] (abrir URL) a [04 CD modo ...]. El nRF reenvia
los eventos con byte 04 a la PC como reporte vendor ID 4: la PC recibe `04 CD 02 00...`.

Solo escribe archivos en disco. No habla con ningun dispositivo.
"""
import hashlib
import pathlib
import sys

import capstone

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "backup-original" / "WL_BEAST_X_V2_8K_Dongle_MH2435_App_v0.0.15.0_20260927-BY1FO-J5.hex"
DST = ROOT / "patched" / SRC.name

ADDR = 0x0801A67C
OLD = bytes.fromhex("40f20312")  # movw r2, #0x103
NEW = bytes.fromhex("4cf60452")  # movw r2, #0xcd04
EXPECTED_SHA = "24acb4582059af98411025d27cbdf2d9c538d9556b016c244303ddcdeb2d226f"


def records(lines):
    base = 0
    for idx, line in enumerate(lines):
        s = line.strip()
        if not s.startswith(":"):
            continue
        b = bytes.fromhex(s[1:])
        n, off, typ = b[0], (b[1] << 8) | b[2], b[3]
        if typ == 0:
            yield idx, base + off, b
        elif typ == 2:
            base = int.from_bytes(b[4:6], "big") << 4
        elif typ == 4:
            base = int.from_bytes(b[4:6], "big") << 16


def main():
    raw = SRC.read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED_SHA:
        sys.exit("El hex original no coincide con el esperado (v0.0.15.0). Abortado.")
    lines = raw.decode("ascii").splitlines(keepends=True)

    patched = 0
    for i in range(len(NEW)):
        a = ADDR + i
        for idx, start, b in records(lines):
            n = b[0]
            if start <= a < start + n:
                rec = bytearray(b)
                pos = 4 + (a - start)
                if rec[pos] != OLD[i]:
                    sys.exit(f"Byte inesperado en 0x{a:08X}: {rec[pos]:02X} (esperaba {OLD[i]:02X})")
                rec[pos] = NEW[i]
                rec[-1] = (-sum(rec[:-1])) & 0xFF
                eol = lines[idx][len(lines[idx].rstrip("\r\n")):]
                lines[idx] = ":" + rec.hex().upper() + eol
                patched += 1
                break
    assert patched == len(NEW), "no se encontraron todos los bytes"

    out = "".join(lines).encode("ascii")
    DST.parent.mkdir(exist_ok=True)
    DST.write_bytes(out)

    # verificacion: releer el hex parcheado, comparar contra el original byte a byte
    def load(text):
        mem = {}
        for _, start, b in records(text.splitlines()):
            for k, x in enumerate(b[4:4 + b[0]]):
                mem[start + k] = x
        return mem

    a_mem, b_mem = load(raw.decode("ascii")), load(out.decode("ascii"))
    diff = sorted(k for k in a_mem if a_mem[k] != b_mem.get(k))
    assert set(a_mem) == set(b_mem), "cambio el mapa de direcciones"
    assert diff == [ADDR + i for i in range(4)], diff
    for line in out.decode("ascii").splitlines():
        rb = bytes.fromhex(line[1:])
        assert sum(rb) & 0xFF == 0, f"checksum mal en {line}"

    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    code = bytes(b_mem[ADDR - 12 + k] for k in range(32))
    print("Diff: 4 bytes en", [hex(x) for x in diff])
    for ins in md.disasm(code, ADDR - 12):
        mark = "  <== parche" if ins.address == ADDR else ""
        print(f"  {ins.address:08X}: {ins.mnemonic:6} {ins.op_str}{mark}")
    print("Escrito:", DST)
    print("SHA256:", hashlib.sha256(out).hexdigest())


if __name__ == "__main__":
    main()
