"""WL Dongle Media: instala la pantalla Media Control en el dongle y queda en segundo plano haciendo de puente.

    python app.py               # abre la ventana
    python app.py --background  # arranca oculto, sin ventana ni icono (asi lo lanza Windows al iniciar)

No usa icono en la bandeja: si ya esta corriendo, abrirlo de nuevo muestra la ventana de la instancia que corre.
"""
import ctypes
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw, ImageFont

import dongle_media
import installer

APP = "WL Dongle Media"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
ACCENT = "#4f8cff"


# ------------------------------------------------------------------ utilidades
SHOW_EVENT = "WLDongleMediaShow"
kernel32 = ctypes.windll.kernel32
kernel32.CreateEventW.restype = ctypes.c_void_p
kernel32.OpenEventW.restype = ctypes.c_void_p
kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
kernel32.SetEvent.argtypes = (ctypes.c_void_p,)
kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)


def single_instance():
    kernel32.CreateMutexW(None, False, "WLDongleMediaMutex")
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def ask_running_instance_to_show():
    """La instancia que ya corre espera este evento y muestra su ventana."""
    h = kernel32.OpenEventW(0x0002, False, SHOW_EVENT)  # EVENT_MODIFY_STATE
    if not h:
        return False
    ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY: que pueda pasar al frente
    kernel32.SetEvent(h)
    kernel32.CloseHandle(h)
    return True


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --background'
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pyw}" "{__file__}" --background'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP)
            return True
    except OSError:
        return False


def set_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, APP)
            except OSError:
                pass


def wlmouse_app_running():
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         creationflags=subprocess.CREATE_NO_WINDOW).stdout.lower()
    return "wl mouse hub" in out


def make_icon(size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, size - 3, size - 3), radius=size // 5, fill=(24, 26, 32, 255))
    try:
        font = ImageFont.truetype("seguisym.ttf", int(size * 0.62))
    except OSError:
        font = ImageFont.load_default()
    d.text((size / 2, size / 2), "♫", font=font, fill=ACCENT, anchor="mm")
    return img


# ------------------------------------------------------------------ app
class App:
    def __init__(self, background):
        self.bridge_on = threading.Event()
        self.bridge_on.set()
        self.busy = False
        threading.Thread(target=dongle_media.listen, kwargs={"active": self.bridge_on.is_set}, daemon=True).start()

        self.root = tk.Tk()
        self.root.title(APP)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self._icon_img = make_icon(64)
        from PIL import ImageTk
        self.root.iconphoto(True, ImageTk.PhotoImage(self._icon_img))
        self.build()

        show_event = kernel32.CreateEventW(None, False, False, SHOW_EVENT)  # auto-reset
        threading.Thread(target=self._wait_show, args=(show_event,), daemon=True).start()

        if background:
            self.root.withdraw()
        else:
            self.root.after(200, self.refresh)

    def build(self):
        f = ttk.Frame(self.root, padding=18)
        f.grid()
        ttk.Label(f, text=APP, font=("Segoe UI Semibold", 15)).grid(sticky="w")
        ttk.Label(f, text="Media Control screen for the WLMouse Beast X V2 8K receiver",
                  foreground="#666").grid(sticky="w", pady=(0, 12))
        self.status = ttk.Label(f, text="Looking for the dongle...", wraplength=380, justify="left")
        self.status.grid(sticky="w", pady=(0, 12))
        row = ttk.Frame(f)
        row.grid(sticky="w")
        self.btn_install = ttk.Button(row, text="Install Media Control", command=self.install)
        self.btn_install.grid(row=0, column=0, padx=(0, 8))
        self.btn_restore = ttk.Button(row, text="Restore official firmware", command=lambda: self.install(restore=True))
        self.btn_restore.grid(row=0, column=1, padx=(0, 8))
        ttk.Button(row, text="Refresh", command=self.refresh).grid(row=0, column=2)
        self.bar = ttk.Progressbar(f, length=380, maximum=100)
        self.bar.grid(sticky="we", pady=(14, 4))
        self.log = ttk.Label(f, text="", foreground="#666", wraplength=380, justify="left")
        self.log.grid(sticky="w")
        self.auto = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(f, text="Start with Windows (runs in the background, no tray icon)", variable=self.auto,
                        command=lambda: set_autostart(self.auto.get())).grid(sticky="w", pady=(12, 0))
        bottom = ttk.Frame(f)
        bottom.grid(sticky="we", pady=(6, 0))
        bottom.columnconfigure(0, weight=1)
        ttk.Label(bottom, text="Closing this window keeps it running in the background.\n"
                               "Open WL Dongle Media again to bring this window back.",
                  foreground="#666", justify="left").grid(row=0, column=0, sticky="w")
        ttk.Button(bottom, text="Quit", command=self.quit).grid(row=0, column=1, sticky="e")

    # --- estado
    def refresh(self):
        if self.busy:
            return
        self.status.config(text="Looking for the dongle...")
        threading.Thread(target=self._refresh, daemon=True).start()

    def _refresh(self):
        self.bridge_on.clear()
        time.sleep(0.6)
        try:
            kind, ver = installer.status()
        finally:
            self.bridge_on.set()
        self.root.after(0, self._show_status, kind, ver)

    def _show_status(self, kind, ver):
        self.kind = kind
        if kind is None:
            text = "Dongle not found. Plug in the Beast X V2 8K receiver and press Refresh."
        elif kind == "update-mode":
            text = "The screen is in update mode (an install didn't finish). Click Install to finish it."
        elif kind == "oficial":
            text = f"Official firmware {ver}. Media Control is not installed yet."
        else:
            text = f"Media Control is installed (firmware {ver}). It keeps running in the background."
        self.status.config(text=text)
        self.btn_install.config(text="Reinstall" if kind == "media-control" else "Install Media Control",
                                state="normal" if kind else "disabled")
        self.btn_restore.config(state="normal" if kind in ("media-control", "update-mode") else "disabled")

    # --- flasheo
    def install(self, restore=False):
        if self.busy:
            return
        if wlmouse_app_running():
            messagebox.showwarning(APP, "Close WL MOUSE HUB (and the WLMouse web driver) first.")
            return
        what = "restore the official screen firmware" if restore else "install the Media Control screen"
        if not messagebox.askokcancel(APP, f"This will {what} on the dongle.\n\n"
                                           "It takes about 10-20 minutes. Don't unplug the dongle or "
                                           "close this program until it says it's done.\n\nContinue?"):
            return
        self.busy = True
        self.btn_install.config(state="disabled")
        self.btn_restore.config(state="disabled")
        self.bar.config(value=0)
        threading.Thread(target=self._install, args=(restore,), daemon=True).start()

    def _install(self, restore):
        def log(msg):
            self.root.after(0, lambda: self.log.config(text=msg))

        def progress(stage, frac):
            self.root.after(0, lambda: self.bar.config(value=(frac if stage == "program" else 1 + frac) * 50))
            log(("Programming" if stage == "program" else "Verifying") + f"... {int(frac * 100)}%  (don't unplug)")

        self.bridge_on.clear()
        time.sleep(1.0)
        ok, err = False, ""
        try:
            installer.install(restore=restore, log=log, progress=progress)
            ok = True
        except installer.InstallError as e:
            err = str(e)
        except Exception as e:  # noqa: BLE001 - mostrar cualquier error en vez de morir en silencio
            err = f"unexpected error: {e}"
        finally:
            self.bridge_on.set()
            self.busy = False
        self.root.after(0, self._install_done, restore, ok, err)

    def _install_done(self, restore, ok, err):
        if ok:
            self.bar.config(value=100)
            if not restore and not autostart_enabled():
                set_autostart(True)
                self.auto.set(True)
            messagebox.showinfo(APP, "Done! The official screen firmware is back." if restore else
                                "Done! Media Control is now the default screen.\n"
                                "Keep this program running (it stays in the background) for the buttons to work.")
        else:
            messagebox.showerror(APP, f"Something went wrong:\n\n{err}\n\nIf the screen stays blank, run the "
                                      "install again (or use the official WLMouse web driver).")
        self.refresh()

    # --- ventana
    def _wait_show(self, event):
        while True:
            if kernel32.WaitForSingleObject(event, 0xFFFFFFFF) == 0:  # INFINITE / WAIT_OBJECT_0
                self.root.after(0, self.show)

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.refresh()

    def hide(self):
        if self.busy:
            messagebox.showwarning(APP, "Wait until the install finishes.")
            return
        self.root.withdraw()

    def quit(self):
        if self.busy:
            messagebox.showwarning(APP, "Wait until the install finishes.")
            return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    if "--write-icon" in sys.argv:  # lo usa el build: python app.py --write-icon app.ico
        make_icon(256).save(sys.argv[sys.argv.index("--write-icon") + 1], sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
        return
    if not single_instance():
        if not ask_running_instance_to_show():
            ctypes.windll.user32.MessageBoxW(None, f"{APP} is already running.", APP, 0x40)
        return
    App(background="--background" in sys.argv).run()


if __name__ == "__main__":
    main()
