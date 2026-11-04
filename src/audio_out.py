"""Salidas de audio de Windows (Core Audio) con ctypes puro: listar las activas, leer y cambiar la predeterminada.

Cambiar la predeterminada usa IPolicyConfig (interfaz no documentada, la misma que usan SoundSwitch y
EarTrumpet). Se cambia para los tres roles (consola, multimedia y comunicaciones).

    python audio_out.py          # lista las salidas activas y marca la actual
"""
import ctypes
import unicodedata
import uuid
from ctypes import POINTER, byref, c_uint, c_ushort, c_void_p, c_wchar_p, wintypes

ole32 = ctypes.OleDLL("ole32")
ole32.CoTaskMemFree.restype = None
ole32.CoTaskMemFree.argtypes = [c_void_p]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_str(cls, s):
        g = cls()
        ctypes.memmove(byref(g), uuid.UUID(s).bytes_le, 16)
        return g


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
                ("ptr", c_void_p), ("pad", c_void_p)]


CLSID_MMDeviceEnumerator = GUID.from_str("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = GUID.from_str("A95664D2-9614-4F35-A746-DE8DB63617E6")
CLSID_PolicyConfig = GUID.from_str("870af99c-171d-4f9e-af0d-e63df40c2bc9")
IID_IPolicyConfig = GUID.from_str("f8679f50-850a-41cf-9c72-430f290290c8")
PKEY_FRIENDLY = PROPERTYKEY(GUID.from_str("a45c254e-df1c-4efd-8020-67d146a850e0"), 14)
PKEY_DESC = PROPERTYKEY(GUID.from_str("a45c254e-df1c-4efd-8020-67d146a850e0"), 2)
PKEY_IFACE = PROPERTYKEY(GUID.from_str("026e516e-b814-414b-83cd-856d6fef4822"), 2)
E_RENDER, DEVICE_STATE_ACTIVE, CLSCTX_ALL, STGM_READ, VT_LPWSTR = 0, 1, 0x17, 0, 31


def _call(obj, index, *args, argtypes=()):
    """Llama al metodo `index` de la vtable de un puntero COM."""
    vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)(vtbl[index])
    return fn(obj, *args)


def _release(obj):
    if obj:
        vtbl = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
        ctypes.WINFUNCTYPE(c_uint, c_void_p)(vtbl[2])(obj)


def _init_com():
    try:
        ole32.CoInitializeEx(None, 0x2)
    except OSError:
        pass  # ya inicializado en este hilo


def _create(clsid, iid):
    p = c_void_p()
    ole32.CoCreateInstance(byref(clsid), None, CLSCTX_ALL, byref(iid), byref(p))
    return p


def _prop(store, key):
    pv = PROPVARIANT()
    _call(store, 5, byref(key), byref(pv), argtypes=(POINTER(PROPERTYKEY), POINTER(PROPVARIANT)))
    s = ctypes.wstring_at(pv.ptr) if pv.vt == VT_LPWSTR and pv.ptr else ""
    ole32.PropVariantClear(byref(pv))
    return s


def _device_info(dev):
    pid = c_wchar_p()
    _call(dev, 5, byref(pid), argtypes=(POINTER(c_wchar_p),))
    dev_id = pid.value
    ole32.CoTaskMemFree(ctypes.cast(pid, c_void_p))
    store = c_void_p()
    _call(dev, 4, STGM_READ, byref(store), argtypes=(wintypes.DWORD, POINTER(c_void_p)))
    info = {"id": dev_id, "friendly": _prop(store, PKEY_FRIENDLY), "desc": _prop(store, PKEY_DESC),
            "iface": _prop(store, PKEY_IFACE)}
    _release(store)
    return info


def list_outputs():
    """Salidas activas (conectadas y habilitadas) en el orden de Windows, con un nombre corto para mostrar."""
    _init_com()
    enum = _create(CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator)
    coll = c_void_p()
    _call(enum, 3, E_RENDER, DEVICE_STATE_ACTIVE, byref(coll), argtypes=(c_uint, wintypes.DWORD, POINTER(c_void_p)))
    n = c_uint()
    _call(coll, 3, byref(n), argtypes=(POINTER(c_uint),))
    out = []
    for i in range(n.value):
        dev = c_void_p()
        _call(coll, 4, i, byref(dev), argtypes=(c_uint, POINTER(c_void_p)))
        out.append(_device_info(dev))
        _release(dev)
    _release(coll)
    _release(enum)
    ifaces = [d["iface"] for d in out]
    for d in out:
        # el nombre del adaptador suele ser el mas claro ("Kiwi Ears-Allegro PRO"); si se repite
        # (ej. dos monitores en la misma placa NVIDIA) se usa el del dispositivo ("XG27AQWMG")
        d["name"] = d["iface"] if d["iface"] and ifaces.count(d["iface"]) == 1 else (d["desc"] or d["friendly"])
    return out


def default_output_id():
    _init_com()
    enum = _create(CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator)
    dev = c_void_p()
    try:
        _call(enum, 4, E_RENDER, 0, byref(dev), argtypes=(c_uint, c_uint, POINTER(c_void_p)))
    except OSError:
        _release(enum)
        return None
    info = _device_info(dev)
    _release(dev)
    _release(enum)
    return info["id"]


def set_default_output(dev_id):
    _init_com()
    pc = _create(CLSID_PolicyConfig, IID_IPolicyConfig)
    for role in (0, 1, 2):  # consola, multimedia, comunicaciones
        _call(pc, 13, dev_id, role, argtypes=(c_wchar_p, c_uint))
    _release(pc)


def next_output():
    """Pasa a la siguiente salida activa despues de la actual. Devuelve su info, o None si no hay salidas."""
    outs = list_outputs()
    if not outs:
        return None
    cur = default_output_id()
    ids = [d["id"] for d in outs]
    nxt = outs[(ids.index(cur) + 1) % len(outs)] if cur in ids else outs[0]
    set_default_output(nxt["id"])
    return nxt


def ascii_name(text, limit=39):
    """Nombre apto para la fuente del dongle: sin acentos, solo ASCII imprimible."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    t = "".join(c for c in t if 32 <= ord(c) < 127).strip()
    return t[:limit]


if __name__ == "__main__":
    cur = default_output_id()
    for d in list_outputs():
        mark = "*" if d["id"] == cur else " "
        print(f"{mark} {ascii_name(d['name']):30}  ({d['friendly']})")
