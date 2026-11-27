import asyncio
import glob
import json
import os
import pwd
import re
import shutil
import subprocess
import threading
import time
import math
import struct
import uuid
import wave

import decky

DEFAULT_SETTINGS = {
    "overlay_enabled": True,
    "position": "top-center",  # top-left | top-center | top-right | bottom-left | bottom-right
                               # (top-right sits under the Quick Access menu)
    "overlay_last_minutes": 0, # only show a timer in its last N minutes (0 = always)
    "notify": True,            # toast when a timer reaches zero
    "shortcut": "l5_r5",       # controller combo opening the timer actions (or "off")
    "alarm_enabled": True,     # alarm sound when a timer reaches zero
    "alarm_repeat": True,      # repeat it while the timer is in overtime
    "alarm_interval": 30,      # seconds between repeats
    "alarm_volume": 0.8,       # 0.0 - 1.0
    "alarm_max_minutes": 5,    # stop repeating after this much overtime (0 = never)
    "overlay_install_prompted": False,  # Decky Overlay install already offered once
}

NAME_MAX = 24               # characters kept in a timer name
PREVIEW_SECONDS = 10        # how long the overlay preview stays on screen

# ---- Decky Overlay --------------------------------------------------------- #
# gamescope shows a single external overlay window: the in-game display is
# delegated to the Decky Overlay plugin (powange/decky-overlay), shared by
# every plugin. See its README for the protocol.
OVERLAY_SOCKET = "/tmp/decky-overlay/overlay.sock"
OVERLAY_PLUGIN_NAME = "Decky Overlay"
OVERLAY_RETRY = 3           # seconds between connection attempts
OVERLAY_MAX_ITEMS = 12      # per block, protocol limit



MAX_DURATION = 24 * 3600    # longest timer (seconds)
MAX_ADD = 12 * 3600         # largest +time / snooze step (seconds)


# ---- Input validation ------------------------------------------------------ #
# The backend runs as root: every value coming from the frontend (or from an
# old state.json) is checked against this whitelist before being used.
def _bool(v):
    if isinstance(v, bool):
        return v
    raise ValueError("not a boolean")


def _choice(*allowed):
    def check(v):
        if v in allowed:
            return v
        raise ValueError(f"not one of {allowed}")
    return check


def _number(lo, hi, cast=float):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("not a number")
        v = cast(v)
        if not lo <= v <= hi:
            raise ValueError(f"out of range [{lo}, {hi}]")
        return v
    return check


SETTINGS_SCHEMA = {
    "overlay_enabled": _bool,
    "position": _choice("top-left", "top-center", "top-right", "bottom-left", "bottom-right"),
    "overlay_last_minutes": _number(0, 600, int),
    "notify": _bool,
    "shortcut": lambda v: _choice("off", *SHORTCUTS)(v),  # SHORTCUTS is defined below
    "alarm_enabled": _bool,
    "alarm_repeat": _bool,
    "alarm_interval": _number(5, 3600, int),
    "alarm_volume": _number(0.0, 1.0),
    "alarm_max_minutes": _number(0, 600, int),
    "overlay_install_prompted": _bool,
}


def _valid_settings(raw):
    """Known keys with valid values only; anything else is dropped and logged."""
    out = {}
    for key, value in (raw.items() if isinstance(raw, dict) else []):
        check = SETTINGS_SCHEMA.get(key)
        if check is None:
            continue
        try:
            out[key] = check(value)
        except (ValueError, TypeError) as e:
            decky.logger.warning(f"Ignoring setting {key}={value!r}: {e}")
    return out


def _duration(v, lo=1, hi=MAX_DURATION):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("duration is not a number")
    return int(max(lo, min(hi, v)))


def _clean_name(name):
    name = " ".join(str(name or "").split())
    return name[:NAME_MAX] or "Timer"

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


# ---- Alarm sound ----------------------------------------------------------- #
# Generated at runtime (no binary asset) and played with PipeWire's pw-play,
# falling back to paplay / aplay, as the desktop user.
ALARM_PLAYERS = ("/usr/bin/pw-play", "/usr/bin/paplay", "/usr/bin/aplay")


def _write_alarm_wav(path):
    """Two groups of three bright beeps (~1.6 s), soft edges to avoid clicks."""
    rate = 44100
    frames = bytearray()

    def tone(freq, dur):
        n = int(rate * dur)
        fade = int(rate * 0.006)
        for i in range(n):
            env = min(1.0, i / fade, (n - 1 - i) / fade)
            # Fundamental + a bit of 2nd harmonic: more piercing over game audio.
            v = 0.75 * math.sin(2 * math.pi * freq * i / rate) + 0.25 * math.sin(4 * math.pi * freq * i / rate)
            frames.extend(struct.pack("<h", int(v * env * 0.9 * 32767)))

    def silence(dur):
        frames.extend(b"\x00\x00" * int(rate * dur))

    for _ in range(2):
        for _ in range(3):
            tone(1760, 0.11)
            silence(0.07)
        silence(0.35)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


# Environment variables set by Decky's bundled (PyInstaller) python that would
# break the system python3 used for the overlay.
_STRIP_ENV = ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH")


def _now() -> float:
    return time.time()


class OverlayClient:
    """Connection to the Decky Overlay plugin's socket (one per Decky Timer)."""

    def __init__(self, on_connect):
        self.on_connect = on_connect
        self.reader = None
        self.writer = None
        self.read_task = None
        self.last_attempt = 0.0
        self.error = ""

    @property
    def connected(self):
        return self.writer is not None

    async def ensure(self):
        """Connects if needed (called from the watch loop, rate limited)."""
        if self.writer is not None or _now() - self.last_attempt < OVERLAY_RETRY:
            return
        self.last_attempt = _now()
        if not os.path.exists(OVERLAY_SOCKET):
            return
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_unix_connection(OVERLAY_SOCKET), 2)
        except Exception as e:
            self.reader = self.writer = None
            self.error = f"Connexion impossible : {e}"
            return
        self.error = ""
        self.send({"op": "hello", "client": "decky-timer", "name": "Decky Timer", "protocol": 1})
        self.read_task = asyncio.get_event_loop().create_task(self._read())
        self.on_connect()

    async def _read(self):
        try:
            while self.reader is not None:
                line = await self.reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("op") == "error":
                    self.error = f"Decky Overlay : {msg.get('error')}"
                    decky.logger.warning(self.error)
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            self._drop()

    def send(self, msg):
        if self.writer is None:
            return
        try:
            self.writer.write((json.dumps(msg) + "\n").encode())
        except Exception:
            self._drop()

    def _drop(self):
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass
        self.reader = self.writer = None

    def close(self):
        if self.read_task:
            self.read_task.cancel()
        self._drop()


def _overlay_installed():
    """True if a plugin named "Decky Overlay" is in Decky's plugins folder."""
    plugins = os.path.join(getattr(decky, "DECKY_HOME", "/home/deck/homebrew"), "plugins")
    for manifest in glob.glob(os.path.join(plugins, "*", "plugin.json")):
        try:
            with open(manifest) as f:
                if json.load(f).get("name") == OVERLAY_PLUGIN_NAME:
                    return True
        except (OSError, ValueError, AttributeError):
            continue
    return False


class Plugin:
    def __init__(self):
        self.state_path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "state.json")
        self.presets = []
        self.timers = []
        self.settings = dict(DEFAULT_SETTINGS)
        self.overlay = OverlayClient(on_connect=self._sync_overlay)
        self.overlay_installed = False
        self.overlay_checked = 0.0
        self.preview_until = 0.0
        self.watch_task = None
        self.loop = None
        self.controller_status = "Initialisation…"
        self.reader_stop = threading.Event()
        self.reader_threads = {}
        self.ignored_nodes = set()   # Deck HID interfaces that send no controller state
        self.alarm_wav = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "alarm.wav")
        self.alarm_proc = None
        self.alarm_last = {}       # timer id -> time of the last alarm
        self.alarm_error = ""
        self.alarm_log = os.path.join(decky.DECKY_PLUGIN_LOG_DIR, "alarm.log")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def _main(self):
        os.makedirs(decky.DECKY_PLUGIN_SETTINGS_DIR, exist_ok=True)
        os.makedirs(decky.DECKY_PLUGIN_LOG_DIR, exist_ok=True)
        self._load()
        self.loop = asyncio.get_event_loop()
        self.watch_task = self.loop.create_task(self._watch())
        threading.Thread(target=self._controller_supervisor, daemon=True).start()
        decky.logger.info("Decky Timer loaded")

    async def _unload(self):
        if self.watch_task:
            self.watch_task.cancel()
        self.reader_stop.set()
        self.overlay.close()
        decky.logger.info("Decky Timer unloaded")

    async def _uninstall(self):
        self.overlay.close()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _load(self):
        try:
            with open(self.state_path, "r") as f:
                data = json.load(f)
            self.presets = self._valid_presets(data.get("presets", []))
            self.timers = self._valid_timers(data.get("timers", []))
            self.settings = {**DEFAULT_SETTINGS, **_valid_settings(data.get("settings", {}))}
        except FileNotFoundError:
            pass
        except Exception as e:
            decky.logger.error(f"Could not read state: {e}")

    @staticmethod
    def _valid_presets(presets):
        out = []
        for p in presets if isinstance(presets, list) else []:
            try:
                out.append({"id": str(p["id"]), "name": _clean_name(p.get("name")),
                            "duration": max(1, int(p.get("duration", 60)))})
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        return out

    @staticmethod
    def _valid_timers(timers):
        """Drops or repairs inconsistent timers from an old or damaged state.json."""
        out = []
        now = _now()
        for t in timers if isinstance(timers, list) else []:
            try:
                duration = max(1, int(t.get("duration", 60)))
                fixed = {
                    "id": str(t["id"]),
                    "name": _clean_name(t.get("name")),
                    "duration": duration,
                    "preset_id": t.get("preset_id") or None,
                    "end_at": None,
                    "remaining": None,
                    "paused": bool(t.get("paused")),
                    "notified": bool(t.get("notified")),
                    "alarm_muted": bool(t.get("alarm_muted")),
                }
                if fixed["paused"]:
                    rem = t.get("remaining")
                    fixed["remaining"] = float(rem) if rem is not None else float(duration)
                else:
                    end_at = t.get("end_at")
                    fixed["end_at"] = float(end_at) if end_at is not None else now + duration
                out.append(fixed)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        return out

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"presets": self.presets, "timers": self.timers, "settings": self.settings}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())  # survive a sudden power-off
        os.replace(tmp, self.state_path)

    def _state(self):
        return {
            "now": _now(),
            "presets": self.presets,
            "timers": self.timers,
            "settings": self.settings,
            "overlay_status": self._overlay_status(),
            "overlay_error": self.overlay.error,
            "controller_status": self.controller_status,
            "alarm_error": self.alarm_error,
            "log_dir": decky.DECKY_PLUGIN_LOG_DIR,
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

    def _preview_active(self):
        return _now() < self.preview_until

    # ------------------------------------------------------------------ #
    # Decky Overlay
    # ------------------------------------------------------------------ #

    def _overlay_status(self):
        if self.overlay.connected:
            return "connected"
        if _now() - self.overlay_checked > 10:
            self.overlay_installed = _overlay_installed()
            self.overlay_checked = _now()
        return "not_running" if self.overlay_installed else "not_installed"

    def _overlay_message(self):
        items = []
        if self._preview_active():
            items.append({"label": "Aperçu Decky Timer", "countdown": {"end_at": self.preview_until + 290}})
        if self.settings.get("overlay_enabled", True):
            limit = int(self.settings.get("overlay_last_minutes", 0)) * 60
            for t in self.timers:
                if t.get("paused"):
                    cd = {"paused": True, "remaining": float(t.get("remaining") or 0)}
                else:
                    cd = {"end_at": float(t.get("end_at") or _now())}
                item = {"label": t["name"], "countdown": cd}
                if limit:
                    item["show_under"] = limit
                items.append(item)
        if not items:
            return {"op": "clear", "id": "timers"}
        return {"op": "set", "id": "timers", "zone": self.settings.get("position", "top-center"),
                "order": 0, "items": items[:OVERLAY_MAX_ITEMS]}

    def _sync_overlay(self):
        """Sends the current timers to Decky Overlay (no-op when not connected)."""
        self.overlay.send(self._overlay_message())

    def _as_desktop_user(self, env):
        """Popen kwargs (and env changes) to run a child as the desktop user."""
        if os.geteuid() != 0:
            return {}
        try:
            pw = pwd.getpwnam(decky.DECKY_USER)
        except Exception as e:
            decky.logger.warning(f"Cannot drop privileges: {e}")
            return {}
        env.update({"HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name,
                    "XDG_RUNTIME_DIR": f"/run/user/{pw.pw_uid}"})
        return {"user": pw.pw_uid, "group": pw.pw_gid}

    # ------------------------------------------------------------------ #
    # Alarm
    # ------------------------------------------------------------------ #

    def _play_alarm(self):
        if self.alarm_proc is not None and self.alarm_proc.poll() is None:
            return  # still playing
        try:
            if not os.path.exists(self.alarm_wav):
                _write_alarm_wav(self.alarm_wav)
                os.chmod(self.alarm_wav, 0o644)
        except Exception as e:
            self.alarm_error = f"Impossible de créer le son : {e}"
            return
        player = next((p for p in ALARM_PLAYERS if os.path.exists(p)), None)
        if player is None:
            self.alarm_error = "Aucun lecteur audio trouvé (pw-play, paplay, aplay)"
            return
        volume = max(0.0, min(1.0, float(self.settings.get("alarm_volume", 0.8))))
        name = os.path.basename(player)
        if name == "pw-play":
            cmd = [player, "--volume", f"{volume:.2f}", self.alarm_wav]
        elif name == "paplay":
            cmd = [player, f"--volume={int(volume * 65536)}", self.alarm_wav]
        else:
            cmd = [player, "-q", self.alarm_wav]
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        try:
            # stderr goes to a file: a pipe nobody reads could block the player.
            with open(self.alarm_log, "w") as err:
                self.alarm_proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL,
                                                   stdout=subprocess.DEVNULL, stderr=err,
                                                   **self._as_desktop_user(env))
            self.alarm_error = ""
        except Exception as e:
            self.alarm_error = f"Lecture du son impossible : {e}"
            decky.logger.error(self.alarm_error)

    def _check_alarm_result(self):
        p = self.alarm_proc
        if p is not None and p.poll() is not None:
            if p.returncode != 0:
                try:
                    with open(self.alarm_log, errors="replace") as f:
                        err = f.read().strip().splitlines()
                except OSError:
                    err = []
                self.alarm_error = f"Le lecteur audio a échoué : {err[-1] if err else p.returncode}"
                decky.logger.error(self.alarm_error)
            self.alarm_proc = None

    def _alarm_tick(self, now):
        self._check_alarm_result()
        if not self.settings.get("alarm_enabled", True):
            return
        interval = max(5, int(self.settings.get("alarm_interval", 30)))
        max_overtime = max(0, int(self.settings.get("alarm_max_minutes", 5))) * 60
        due = False
        active = set()
        for t in self.timers:
            if t.get("paused") or not t.get("end_at") or t["end_at"] > now:
                continue
            if t.get("alarm_muted"):
                continue
            # Never ring for a timer forgotten for long (or expired while off).
            if max_overtime and now - t["end_at"] > max_overtime:
                continue
            active.add(t["id"])
            last = self.alarm_last.get(t["id"])
            if last is None or (self.settings.get("alarm_repeat", True) and now - last >= interval):
                self.alarm_last[t["id"]] = now
                due = True
        # Forget stopped / paused / restarted timers.
        self.alarm_last = {k: v for k, v in self.alarm_last.items() if k in active}
        if due:
            self._play_alarm()

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
                self._alarm_tick(now)
                # End of the overlay preview: hide it (or show the real timers).
                if self.preview_until and now >= self.preview_until:
                    self.preview_until = 0.0
                    self._sync_overlay()
                await self.overlay.ensure()  # (re)connects to Decky Overlay
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
            if self.settings.get("shortcut") not in SHORTCUTS:
                # Shortcut off: readers exit by themselves, start none (no
                # 250 Hz wake-ups for nothing).
                self.controller_status = "Raccourci désactivé"
                self.reader_stop.wait(2)
                continue
            all_nodes = _find_deck_hidraw()
            if all_nodes != getattr(self, "_last_nodes", None):
                # Devices renumbered (resume, reconnect): re-check every node.
                self.ignored_nodes.clear()
                self._last_nodes = all_nodes
            nodes = [n for n in all_nodes if n not in self.ignored_nodes]
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
        other_reports = 0
        try:
            while not self.reader_stop.is_set():
                report = os.read(fd, 64)
                if len(report) != 64 or report[0] != 1 or report[1] != 0 or report[2] != 9:
                    other_reports += 1
                    if other_reports >= 20 and held_since is None and not fired:
                        # Keyboard/mouse interface of the Deck: not the controller.
                        self.ignored_nodes.add(node)
                        decky.logger.info(f"Ignoring {node}: no controller reports")
                        return
                    continue
                other_reports = -10**9  # a controller node is never ignored
                combo = SHORTCUTS.get(self.settings.get("shortcut", ""))
                if not combo:
                    return  # shortcut turned off: stop reading
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
        duration = _duration(duration)
        if preset_id and not any(p["id"] == preset_id for p in self.presets):
            preset_id = ""
        now = _now()
        self.timers.append({
            "id": uuid.uuid4().hex[:10],
            "name": _clean_name(name),
            "duration": duration,
            "preset_id": preset_id or None,
            "end_at": now + duration,
            "remaining": None,
            "paused": False,
            "notified": False,
            "alarm_muted": False,
        })
        return self._changed()

    async def pause_timer(self, timer_id: str):
        t = self._find_timer(timer_id)
        if t and not t["paused"]:
            t["remaining"] = (t.get("end_at") or _now()) - _now()
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
            t["alarm_muted"] = False
        return self._changed()

    async def add_time(self, timer_id: str, seconds: float):
        """+1 / +5 min, or -1 / -5 min with a negative value.

        Adding time back above zero lets the timer ring again; removing time
        past zero makes it end now (notification and alarm as usual).
        """
        t = self._find_timer(timer_id)
        if t:
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                raise ValueError("seconds is not a number")
            seconds = int(max(-MAX_ADD, min(MAX_ADD, seconds)))
            if seconds == 0:
                return self._state()
            if t["paused"]:
                t["remaining"] = float(t["remaining"] or 0) + seconds
                remaining = t["remaining"]
            else:
                t["end_at"] = float(t["end_at"] or _now()) + seconds
                remaining = t["end_at"] - _now()
            if seconds > 0 and remaining > 0:
                t["notified"] = False
                t["alarm_muted"] = False
        return self._changed()

    async def snooze_timer(self, timer_id: str, seconds: float = 300):
        """"Rappel dans 5 min": the timer restarts from that delay, running."""
        t = self._find_timer(timer_id)
        if t:
            t["end_at"] = _now() + _duration(seconds, 1, MAX_ADD)
            t["remaining"] = None
            t["paused"] = False
            t["notified"] = False
            t["alarm_muted"] = False
        return self._changed()

    async def preview_overlay(self):
        self.preview_until = _now() + PREVIEW_SECONDS
        self._sync_overlay()
        return self._state()

    async def overlay_install_prompted(self):
        """Remembers that the Decky Overlay install was offered (once only)."""
        self.settings["overlay_install_prompted"] = True
        self._save()
        self.overlay_checked = 0.0
        return self._state()

    async def mute_alarm(self, timer_id: str):
        t = self._find_timer(timer_id)
        if t:
            t["alarm_muted"] = True
        return self._changed()

    async def stop_timer(self, timer_id: str):
        self.timers = [t for t in self.timers if t["id"] != timer_id]
        return self._changed()

    async def stop_all(self):
        self.timers = []
        return self._changed()

    # Presets ---------------------------------------------------------- #

    async def save_preset(self, preset_id: str, name: str, duration: float):
        name = _clean_name(name)
        duration = _duration(duration)
        existing = next((p for p in self.presets if p["id"] == preset_id), None) if preset_id else None
        if existing:
            existing["name"] = name
            existing["duration"] = duration
            saved_id = existing["id"]
        else:
            saved_id = uuid.uuid4().hex[:10]
            self.presets.append({"id": saved_id, "name": name, "duration": duration})
        # The frontend needs the id to launch the timer linked to this preset.
        return {**self._changed(), "saved_id": saved_id}

    async def delete_preset(self, preset_id: str):
        self.presets = [p for p in self.presets if p["id"] != preset_id]
        return self._changed()

    async def move_preset(self, preset_id: str, delta: int):
        idx = next((i for i, p in enumerate(self.presets) if p["id"] == preset_id), None)
        if idx is not None:
            delta = 1 if int(delta) > 0 else -1  # one step at a time
            new = max(0, min(len(self.presets) - 1, idx + delta))
            self.presets.insert(new, self.presets.pop(idx))
        return self._changed()

    # Settings --------------------------------------------------------- #

    async def test_alarm(self):
        self.alarm_proc = None if (self.alarm_proc and self.alarm_proc.poll() is not None) else self.alarm_proc
        self._play_alarm()
        await asyncio.sleep(0.3)
        self._check_alarm_result()
        return self._state()

    async def save_settings(self, settings: dict):
        self.settings.update(_valid_settings(settings))
        return self._changed()
