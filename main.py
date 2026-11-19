import asyncio
import glob
import json
import os
import pwd
import shutil
import subprocess
import threading
import time
import uuid

import decky

DEFAULT_SETTINGS = {
    "overlay_enabled": True,
    "position": "top-right",   # top-left | top-center | top-right | bottom-left | bottom-right
    "size": "medium",          # small | medium | large | xlarge
    "opacity": 0.75,
    "notify": True,            # toast when a timer reaches zero
    "display": ":0",           # gamescope's Xwayland used by Steam
    "shortcut": "l5_r5",       # controller combo opening the timer actions (or "off")
}

# ---- Controller shortcut -------------------------------------------------- #
# The Deck controller is read from its raw HID reports (hidraw), like
# powange/decky-macro does: every hidraw reader gets its own copy of each
# report, so Steam and the game are not affected. Needs the "root" flag.
# Report layout (Linux hid-steam.c): 64 bytes, data[0]=1, data[1]=0,
# data[2]=9 (ID_CONTROLLER_DECK_STATE); buttons as (byte, bit):
DECK_BUTTONS = {
    "A": (8, 7), "B": (8, 5), "X": (8, 6), "Y": (8, 4),
    "SELECT": (9, 4), "STEAM": (9, 5), "START": (9, 6),
    "L5": (9, 7), "R5": (10, 0), "L4": (13, 1), "R4": (13, 2),
    "QAM": (14, 2),
}
SHORTCUTS = {
    "l5_r5": ("L5", "R5"),
    "l4_r4": ("L4", "R4"),
    "select_start": ("SELECT", "START"),
}
SHORTCUT_HOLD = 0.4      # seconds the combo must be held
SHORTCUT_COOLDOWN = 1.5  # seconds between two triggers
VALVE_DECK_HID_ID = ":000028DE:00001205"


def _find_deck_hidraw():
    nodes = []
    for uevent in glob.glob("/sys/class/hidraw/*/device/uevent"):
        try:
            with open(uevent) as f:
                if VALVE_DECK_HID_ID in f.read().upper():
                    nodes.append("/dev/" + uevent.split("/")[4])
        except OSError:
            pass
    return sorted(nodes)


def _pressed(report, name):
    byte, bit = DECK_BUTTONS[name]
    return bool(report[byte] & (1 << bit))


# Environment variables set by Decky's bundled (PyInstaller) python that would
# break the system python3 used for the overlay.
_STRIP_ENV = ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH")


def _now() -> float:
    return time.time()


class Plugin:
    def __init__(self):
        self.state_path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "state.json")
        self.overlay_script = os.path.join(decky.DECKY_PLUGIN_DIR, "overlay", "overlay.py")
        self.overlay_log = os.path.join(decky.DECKY_PLUGIN_LOG_DIR, "overlay.log")
        self.presets = []
        self.timers = []
        self.settings = dict(DEFAULT_SETTINGS)
        self.proc = None
        self.overlay_error = ""
        self.overlay_failures = []
        self.watch_task = None
        self.loop = None
        self.controller_status = "Initialisation…"
        self.reader_stop = threading.Event()
        self.reader_threads = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def _main(self):
        os.makedirs(decky.DECKY_PLUGIN_SETTINGS_DIR, exist_ok=True)
        os.makedirs(decky.DECKY_PLUGIN_LOG_DIR, exist_ok=True)
        self._load()
        self._sync_overlay()
        self.loop = asyncio.get_event_loop()
        self.watch_task = self.loop.create_task(self._watch())
        threading.Thread(target=self._controller_supervisor, daemon=True).start()
        decky.logger.info("Decky Timer loaded")

    async def _unload(self):
        if self.watch_task:
            self.watch_task.cancel()
        self.reader_stop.set()
        self._stop_overlay()
        decky.logger.info("Decky Timer unloaded")

    async def _uninstall(self):
        self._stop_overlay()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _load(self):
        try:
            with open(self.state_path, "r") as f:
                data = json.load(f)
            self.presets = data.get("presets", [])
            self.timers = data.get("timers", [])
            self.settings = {**DEFAULT_SETTINGS, **data.get("settings", {})}
        except FileNotFoundError:
            pass
        except Exception as e:
            decky.logger.error(f"Could not read state: {e}")

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"presets": self.presets, "timers": self.timers, "settings": self.settings}, f, indent=2)
        os.replace(tmp, self.state_path)

    def _state(self):
        return {
            "now": _now(),
            "presets": self.presets,
            "timers": self.timers,
            "settings": self.settings,
            "overlay_running": self.proc is not None and self.proc.poll() is None,
            "overlay_error": self.overlay_error,
            "controller_status": self.controller_status,
        }

    def _changed(self):
        self._save()
        self._sync_overlay()
        return self._state()

    def _find_timer(self, timer_id):
        return next((t for t in self.timers if t["id"] == timer_id), None)

    # ------------------------------------------------------------------ #
    # Overlay process
    # ------------------------------------------------------------------ #

    def _overlay_wanted(self):
        return bool(self.settings.get("overlay_enabled")) and len(self.timers) > 0

    def _overlay_payload(self):
        timers = [
            {k: t.get(k) for k in ("id", "name", "end_at", "remaining", "paused")}
            for t in self.timers
        ]
        settings = {k: self.settings.get(k) for k in ("position", "size", "opacity")}
        return json.dumps({"timers": timers, "settings": settings}) + "\n"

    def _start_overlay(self):
        python = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else shutil.which("python3")
        if not python:
            self.overlay_error = "python3 introuvable sur le système"
            return
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        env["DISPLAY"] = self.settings.get("display") or ":0"
        # The backend runs as root (needed for hidraw); the overlay runs as the
        # desktop user, who owns the gamescope X session.
        user_kwargs = {}
        if os.geteuid() == 0:
            try:
                pw = pwd.getpwnam(decky.DECKY_USER)
                user_kwargs = {"user": pw.pw_uid, "group": pw.pw_gid}
                env.update({"HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name,
                            "XDG_RUNTIME_DIR": f"/run/user/{pw.pw_uid}"})
            except Exception as e:
                decky.logger.warning(f"Cannot drop privileges for overlay: {e}")
        try:
            log = open(self.overlay_log, "a")
            log.write(f"--- start {time.strftime('%Y-%m-%d %H:%M:%S')} DISPLAY={env['DISPLAY']}\n")
            log.flush()
            self.proc = subprocess.Popen(
                [python, "-u", self.overlay_script],
                stdin=subprocess.PIPE, stdout=log, stderr=log, env=env,
                cwd=os.path.dirname(self.overlay_script), **user_kwargs,
            )
            self.proc_started = _now()
            log.close()
        except Exception as e:
            self.proc = None
            self.overlay_error = f"Lancement impossible : {e}"
            decky.logger.error(self.overlay_error)

    def _stop_overlay(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=1.5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def _sync_overlay(self):
        """Start/stop the overlay as needed and push it the current timers."""
        if not self._overlay_wanted():
            self._stop_overlay()
            return
        if self.proc is not None and self.proc.poll() is not None:
            self._overlay_died()
        if self.proc is None:
            # Give up after 3 crashes in a minute; settings changes retry.
            recent = [t for t in self.overlay_failures if _now() - t < 60]
            if len(recent) >= 3:
                return
            self._start_overlay()
        if self.proc is not None:
            try:
                self.proc.stdin.write(self._overlay_payload().encode())
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._overlay_died()

    def _overlay_died(self):
        code = self.proc.poll() if self.proc else None
        self.proc = None
        self.overlay_failures.append(_now())
        tail = ""
        try:
            with open(self.overlay_log, "r") as f:
                lines = [l.strip() for l in f.readlines() if l.strip()]
            tail = lines[-1] if lines else ""
        except Exception:
            pass
        self.overlay_error = f"L'overlay s'est arrêté (code {code}). {tail}".strip()
        decky.logger.error(self.overlay_error)

    async def _watch(self):
        while True:
            try:
                await asyncio.sleep(0.5)
                now = _now()
                fired = []
                for t in self.timers:
                    if not t.get("paused") and not t.get("notified") and t.get("end_at") and t["end_at"] <= now:
                        t["notified"] = True
                        fired.append(t)
                if fired:
                    self._save()
                    if self.settings.get("notify", True):
                        for t in fired:
                            await decky.emit("timer_finished", {"id": t["id"], "name": t["name"]})
                # Restart a crashed overlay.
                if self._overlay_wanted() and (self.proc is None or self.proc.poll() is not None):
                    self._sync_overlay()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky.logger.error(f"watch loop: {e}")

    # ------------------------------------------------------------------ #
    # Controller shortcut (hidraw)
    # ------------------------------------------------------------------ #

    def _controller_supervisor(self):
        """Keeps one reader thread per Deck hidraw node (survives suspend)."""
        while not self.reader_stop.is_set():
            nodes = _find_deck_hidraw()
            for node in nodes:
                t = self.reader_threads.get(node)
                if t is None or not t.is_alive():
                    t = threading.Thread(target=self._read_controller, args=(node,), daemon=True)
                    self.reader_threads[node] = t
                    t.start()
            alive = [n for n, t in self.reader_threads.items() if t.is_alive()]
            if not nodes:
                self.controller_status = "Manette du Deck introuvable"
            elif not alive:
                if os.geteuid() != 0:
                    self.controller_status = "Lecture de la manette impossible (le plugin doit tourner en root)"
                else:
                    self.controller_status = "Lecture de la manette impossible (voir les logs)"
            else:
                self.controller_status = "Manette détectée"
            self.reader_stop.wait(5)

    def _read_controller(self, node):
        try:
            fd = os.open(node, os.O_RDONLY)
        except OSError as e:
            decky.logger.warning(f"Cannot open {node}: {e}")
            return
        decky.logger.info(f"Reading controller from {node}")
        held_since = None
        fired = False
        try:
            while not self.reader_stop.is_set():
                report = os.read(fd, 64)
                if len(report) != 64 or report[0] != 1 or report[1] != 0 or report[2] != 9:
                    continue
                combo = SHORTCUTS.get(self.settings.get("shortcut", ""))
                if not combo:
                    held_since = None
                    continue
                if all(_pressed(report, b) for b in combo):
                    now = time.monotonic()
                    if held_since is None:
                        held_since = now
                        fired = False
                    elif not fired and now - held_since >= SHORTCUT_HOLD:
                        fired = True
                        self._shortcut_triggered()
                else:
                    held_since = None
        except OSError as e:
            decky.logger.info(f"Controller reader {node} stopped: {e}")
        finally:
            os.close(fd)

    _last_shortcut = 0.0

    def _shortcut_triggered(self):
        now = time.monotonic()
        if now - self._last_shortcut < SHORTCUT_COOLDOWN:
            return
        self._last_shortcut = now
        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(decky.emit("open_quick_controls"), self.loop)

    # ------------------------------------------------------------------ #
    # Frontend API
    # ------------------------------------------------------------------ #

    async def get_state(self):
        return self._state()

    async def get_version(self) -> str:
        version = os.environ.get("DECKY_PLUGIN_VERSION")
        if version:
            return version
        try:
            with open(os.path.join(decky.DECKY_PLUGIN_DIR, "package.json"), "r") as f:
                return json.load(f).get("version", "?")
        except Exception:
            return "?"

    # Timers ----------------------------------------------------------- #

    async def start_timer(self, name: str, duration: float, preset_id: str = ""):
        duration = max(1, int(duration))
        now = _now()
        self.timers.append({
            "id": uuid.uuid4().hex[:10],
            "name": (name or "").strip() or "Timer",
            "duration": duration,
            "preset_id": preset_id or None,
            "end_at": now + duration,
            "remaining": None,
            "paused": False,
            "notified": False,
        })
        return self._changed()

    async def pause_timer(self, timer_id: str):
        t = self._find_timer(timer_id)
        if t and not t["paused"]:
            t["remaining"] = t["end_at"] - _now()
            t["end_at"] = None
            t["paused"] = True
        return self._changed()

    async def resume_timer(self, timer_id: str):
        t = self._find_timer(timer_id)
        if t and t["paused"]:
            t["end_at"] = _now() + float(t["remaining"] or 0)
            t["remaining"] = None
            t["paused"] = False
        return self._changed()

    async def restart_timer(self, timer_id: str):
        t = self._find_timer(timer_id)
        if t:
            t["end_at"] = _now() + t["duration"]
            t["remaining"] = None
            t["paused"] = False
            t["notified"] = False
        return self._changed()

    async def stop_timer(self, timer_id: str):
        self.timers = [t for t in self.timers if t["id"] != timer_id]
        return self._changed()

    async def stop_all(self):
        self.timers = []
        return self._changed()

    # Presets ---------------------------------------------------------- #

    async def save_preset(self, preset_id: str, name: str, duration: float):
        name = (name or "").strip() or "Timer"
        duration = max(1, int(duration))
        existing = next((p for p in self.presets if p["id"] == preset_id), None) if preset_id else None
        if existing:
            existing["name"] = name
            existing["duration"] = duration
        else:
            self.presets.append({"id": uuid.uuid4().hex[:10], "name": name, "duration": duration})
        return self._changed()

    async def delete_preset(self, preset_id: str):
        self.presets = [p for p in self.presets if p["id"] != preset_id]
        return self._changed()

    async def move_preset(self, preset_id: str, delta: int):
        idx = next((i for i, p in enumerate(self.presets) if p["id"] == preset_id), None)
        if idx is not None:
            new = max(0, min(len(self.presets) - 1, idx + int(delta)))
            self.presets.insert(new, self.presets.pop(idx))
        return self._changed()

    # Settings --------------------------------------------------------- #

    async def save_settings(self, settings: dict):
        old_display = self.settings.get("display")
        for key in DEFAULT_SETTINGS:
            if key in settings:
                self.settings[key] = settings[key]
        # A settings change is also a manual retry after overlay crashes.
        self.overlay_failures = []
        self.overlay_error = ""
        if self.settings.get("display") != old_display:
            self._stop_overlay()
        return self._changed()
