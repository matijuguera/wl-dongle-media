"""Estado de la PC para la pantalla Audio/Mezclador del dongle (ctypes puro, sin dependencias):

  * Spotify: cancion y si esta sonando, leidos del titulo de su ventana ("Artista - Cancion" mientras
    suena; "Spotify Premium"/"Spotify Free"/"Spotify" en pausa).
  * Microfono: mute del microfono predeterminado de Windows (IAudioEndpointVolume).
  * Mezclador: volumen y mute por app (sesiones de audio, ISimpleAudioVolume), Spotify primero.

    python pc_state.py     # muestra todo lo que ve
"""
import ctypes
import os
from ctypes import POINTER, byref, c_float, c_int, c_void_p, wintypes

import audio_out as ao

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

IID_IAudioSessionManager2 = ao.GUID.from_str("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F")
IID_IAudioSessionControl2 = ao.GUID.from_str("bfb7ff88-7239-4fc9-8fa2-07c950be9c6d")
IID_ISimpleAudioVolume = ao.GUID.from_str("87CE5498-68D6-44E5-9215-6DA47EF883D8")
IID_IAudioEndpointVolume = ao.GUID.from_str("5CDF2C82-841E-4546-9722-0CF74078229A")
E_CAPTURE = 1
STEP = 0.02

# icono por app (lo dibuja el dongle): 0 generico, 1 Spotify, 2 Discord, 3 navegador, 4 juego
APP_INFO = {
    "spotify.exe": ("Spotify", 1),
    "discord.exe": ("Discord", 2),
    "chrome.exe": ("Chrome", 3),
    "msedge.exe": ("Edge", 3),
    "firefox.exe": ("Firefox", 3),
    "opera.exe": ("Opera", 3),
    "brave.exe": ("Brave", 3),
    "league of legends.exe": ("League of Legends", 4),
}
SPOTIFY_IDLE_TITLES = {"spotify", "spotify premium", "spotify free"}


def process_name(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        return os.path.basename(buf.value) if kernel32.QueryFullProcessImageNameW(h, 0, buf, byref(size)) else ""
    finally:
        kernel32.CloseHandle(h)


# ------------------------------------------------------------------ Spotify (titulo de ventana)
def spotify_now_playing():
    """(sonando: bool|None, titulo: str). None si Spotify no esta abierto."""
    titles = []
    proc_cache = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, byref(pid))
        name = proc_cache.setdefault(pid.value, process_name(pid.value).lower())
        if name == "spotify.exe":
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            titles.append(buf.value.strip())
        return True

    user32.EnumWindows(cb, 0)
    titles = [t for t in titles if t]
    if not titles:
        return None, ""
    playing = [t for t in titles if t.lower() not in SPOTIFY_IDLE_TITLES and " - " in t]
    if playing:
        return True, playing[0]
    return False, ""


def spotify_audio_active():
    """True si alguna sesion de audio de Spotify esta sonando (AudioSessionStateActive)."""
    ss = _sessions(with_controls=True)
    try:
        for exe, _, ctl in ss:
            if exe == "spotify.exe":
                st = c_int()
                ao._call(ctl, 3, byref(st), argtypes=(POINTER(c_int),))
                if st.value == 1:
                    return True
        return False
    finally:
        for _, s, ctl in ss:
            ao._release(s)
            ao._release(ctl)


def spotify_status():
    """('cerrado'|'pausado'|'sonando', titulo). Cerrado = sin ventana visible (cerrado o en la bandeja)."""
    titles = []
    proc_cache = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, byref(pid))
        if proc_cache.setdefault(pid.value, process_name(pid.value).lower()) == "spotify.exe":
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            titles.append(buf.value.strip())
        return True

    user32.EnumWindows(cb, 0)
    if not titles:
        return "cerrado", ""
    song = next((t for t in titles if t.lower() not in SPOTIFY_IDLE_TITLES and " - " in t), "")
    if song and spotify_audio_active():
        return "sonando", song
    return "pausado", song


# ------------------------------------------------------------------ microfono
def _default_capture_volume():
    ao._init_com()
    enum = ao._create(ao.CLSID_MMDeviceEnumerator, ao.IID_IMMDeviceEnumerator)
    dev, vol = c_void_p(), c_void_p()
    try:
        ao._call(enum, 4, E_CAPTURE, 0, byref(dev), argtypes=(ctypes.c_uint, ctypes.c_uint, POINTER(c_void_p)))
        ao._call(dev, 3, byref(IID_IAudioEndpointVolume), ao.CLSCTX_ALL, None, byref(vol),
                 argtypes=(POINTER(ao.GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p)))
    except OSError:
        vol = c_void_p()
    finally:
        ao._release(dev)
        ao._release(enum)
    return vol


def mic_muted():
    """True/False, o None si no hay microfono."""
    vol = _default_capture_volume()
    if not vol:
        return None
    try:
        m = wintypes.BOOL()
        ao._call(vol, 15, byref(m), argtypes=(POINTER(wintypes.BOOL),))
        return bool(m.value)
    finally:
        ao._release(vol)


def toggle_mic():
    vol = _default_capture_volume()
    if not vol:
        return None
    try:
        m = wintypes.BOOL()
        ao._call(vol, 15, byref(m), argtypes=(POINTER(wintypes.BOOL),))
        ao._call(vol, 14, wintypes.BOOL(not m.value), None, argtypes=(wintypes.BOOL, c_void_p))
        return not m.value
    finally:
        ao._release(vol)


# ------------------------------------------------------------------ mezclador
def _qi(obj, iid):
    out = c_void_p()
    ao._call(obj, 0, byref(iid), byref(out), argtypes=(POINTER(ao.GUID), POINTER(c_void_p)))
    return out


def _sessions(with_controls=False):
    """[(exe_en_minusculas, ISimpleAudioVolume)] de todas las salidas activas (sin sonidos del sistema).
    Con with_controls=True: [(exe, ISimpleAudioVolume, IAudioSessionControl)]."""
    ao._init_com()
    enum = ao._create(ao.CLSID_MMDeviceEnumerator, ao.IID_IMMDeviceEnumerator)
    coll = c_void_p()
    ao._call(enum, 3, ao.E_RENDER, ao.DEVICE_STATE_ACTIVE, byref(coll),
             argtypes=(ctypes.c_uint, wintypes.DWORD, POINTER(c_void_p)))
    n = ctypes.c_uint()
    ao._call(coll, 3, byref(n), argtypes=(POINTER(ctypes.c_uint),))
    out = []
    for i in range(n.value):
        dev, mgr = c_void_p(), c_void_p()
        ao._call(coll, 4, i, byref(dev), argtypes=(ctypes.c_uint, POINTER(c_void_p)))
        try:
            ao._call(dev, 3, byref(IID_IAudioSessionManager2), ao.CLSCTX_ALL, None, byref(mgr),
                     argtypes=(POINTER(ao.GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p)))
            senum = c_void_p()
            ao._call(mgr, 5, byref(senum), argtypes=(POINTER(c_void_p),))
            cnt = c_int()
            ao._call(senum, 3, byref(cnt), argtypes=(POINTER(c_int),))
            for k in range(cnt.value):
                ctl = c_void_p()
                ao._call(senum, 4, k, byref(ctl), argtypes=(c_int, POINTER(c_void_p)))
                ctl2 = _qi(ctl, IID_IAudioSessionControl2)
                pid = wintypes.DWORD()
                ao._call(ctl2, 14, byref(pid), argtypes=(POINTER(wintypes.DWORD),))
                name = process_name(pid.value).lower() if pid.value else ""
                if name and with_controls:
                    out.append((name, _qi(ctl, IID_ISimpleAudioVolume), ctl))
                    ctl = None
                elif name:
                    out.append((name, _qi(ctl, IID_ISimpleAudioVolume)))
                ao._release(ctl2)
                if ctl:
                    ao._release(ctl)
            ao._release(senum)
        except OSError:
            pass
        finally:
            ao._release(mgr)
            ao._release(dev)
    ao._release(coll)
    ao._release(enum)
    return out


def _free(sessions):
    for _, s in sessions:
        ao._release(s)


def app_display(exe):
    if exe in APP_INFO:
        return APP_INFO[exe]
    stem = exe[:-4] if exe.endswith(".exe") else exe
    return stem[:1].upper() + stem[1:], 0


def list_apps():
    """Apps con sesion de audio, sin repetir, Spotify primero y el resto por nombre."""
    ss = _sessions()
    try:
        exes = sorted({e for e, _ in ss}, key=lambda e: (e != "spotify.exe", app_display(e)[0].lower()))
    finally:
        _free(ss)
    return exes


def app_volume(exe):
    """(volumen 0..100, muteada) o (None, False) si la app no tiene sesion."""
    ss = _sessions()
    try:
        mine = [s for e, s in ss if e == exe]
        if not mine:
            return None, False
        v, m = c_float(), wintypes.BOOL()
        ao._call(mine[0], 4, byref(v), argtypes=(POINTER(c_float),))
        ao._call(mine[0], 6, byref(m), argtypes=(POINTER(wintypes.BOOL),))
        return round(v.value * 100), bool(m.value)
    finally:
        _free(ss)


def change_app_volume(exe, steps):
    ss = _sessions()
    try:
        mine = [s for e, s in ss if e == exe]
        if not mine:
            return None
        v = c_float()
        ao._call(mine[0], 4, byref(v), argtypes=(POINTER(c_float),))
        new = min(100, max(0, round(v.value * 100) + round(steps * STEP * 100))) / 100
        for s in mine:
            ao._call(s, 3, c_float(new), None, argtypes=(c_float, c_void_p))
        return round(new * 100)
    finally:
        _free(ss)


def toggle_app_mute(exe):
    ss = _sessions()
    try:
        mine = [s for e, s in ss if e == exe]
        if not mine:
            return None
        m = wintypes.BOOL()
        ao._call(mine[0], 6, byref(m), argtypes=(POINTER(wintypes.BOOL),))
        for s in mine:
            ao._call(s, 5, wintypes.BOOL(not m.value), None, argtypes=(wintypes.BOOL, c_void_p))
        return not m.value
    finally:
        _free(ss)


if __name__ == "__main__":
    print("Spotify:", spotify_now_playing())
    print("Micrófono muteado:", mic_muted())
    for e in list_apps():
        print(f"  {app_display(e)[0]:20} icono {app_display(e)[1]}  vol/mute {app_volume(e)}")
