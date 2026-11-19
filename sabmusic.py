#!/usr/bin/env python3
"""SabMusic - a tiny music library launcher.

Scans your chosen folders for audio files and shows them in one flat,
searchable list. Tracks are identified by their plain filename only (no
artist/album metadata, no artwork). The list is sorted by "Recently added"
(newest file modification time first) by default. Plays with mpv (or VLC),
with playlist support and permanent delete.

No third-party Python packages required. Just Python 3 + tkinter + mpv.
"""

import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "SabMusic"

AUDIO_EXTS = {
    ".mp3", ".m4a", ".m4b", ".aac", ".flac", ".wav",
    ".opus", ".wma", ".aiff", ".aif", ".alac", ".ape", ".wv", ".mpc",
    ".mka", ".ac3", ".dts", ".amr", ".au", ".ra", ".spx", ".mid", ".midi",
}

SKIP_DIR_NAMES = {
    "node_modules", "__pycache__", "$recycle.bin",
    "system volume information", "recovery", "windows", "winnt",
}

# Dark palette
BG = "#1e1f22"
BG_PANEL = "#2b2d31"
BG_FIELD = "#141518"
BG_HOVER = "#3a3d45"
FG = "#e6e6e6"
FG_MUTED = "#9aa0a6"
ACCENT = "#4f8cff"
ACCENT_TEXT = "#ffffff"
DANGER = "#ff6b6b"
BORDER = "#3a3d45"
ROW_ALT = "#2a2c31"


def _config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "sabmusic"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "sabmusic"


def _cache_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "sabmusic"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "sabmusic"


CONFIG_FILE = _config_dir() / "config.json"
CACHE_FILE = _cache_dir() / "library.json"
PLAYS_FILE = _cache_dir() / "plays.json"
DEFAULT_MIN_MB = 0
CONFIG_VERSION = 1
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
BUNDLED_MPV_CONFIG = APP_DIR / "sabmusic-mpv-config"


def _default_config() -> dict:
    return {
        "roots": [str(Path.home())],
        "min_size_mb": DEFAULT_MIN_MB,
        "mpv_path": "",
        "vlc_path": "",
        "player": "vlc",
        "scan_on_startup": True,
        "ui_version": CONFIG_VERSION,
    }


def load_config() -> dict:
    cfg = _default_config()
    migrated = False
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            cfg.update(data)
            if int(data.get("ui_version") or 0) < CONFIG_VERSION:
                cfg["ui_version"] = CONFIG_VERSION
                migrated = True
    except (OSError, ValueError, TypeError):
        pass
    if migrated:
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError:
        pass


def save_cache(items: list) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"saved": time.time(), "items": items}, fh)
    except OSError:
        pass


def load_cache() -> list:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        items = data.get("items", [])
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict) and i.get("path")]
    except (OSError, ValueError):
        pass
    return []


def load_plays() -> dict:
    try:
        with open(PLAYS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_plays(plays: dict) -> None:
    try:
        PLAYS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(PLAYS_FILE, "w", encoding="utf-8") as fh:
            json.dump(plays, fh)
    except OSError:
        pass


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def added_label(mtime) -> str:
    try:
        value = float(mtime)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))


def natural_key(text: str) -> list:
    return [(1, int(part)) if part.isdigit() else (0, part.lower())
            for part in re.split(r"(\d+)", text or "")]


def find_mpv(cfg: dict) -> str:
    configured = (cfg.get("mpv_path") or "").strip()
    if configured and os.path.isfile(configured):
        return configured
    exe = shutil.which("mpv") or shutil.which("mpv.exe")
    if exe:
        return exe
    if os.name == "nt":
        candidates = [
            r"C:\Program Files\mpv\mpv.exe",
            r"C:\Program Files (x86)\mpv\mpv.exe",
            r"C:\mpv\mpv.exe",
            str(Path.home() / "mpv" / "mpv.exe"),
            str(Path.home() / "scoop" / "apps" / "mpv" / "current" / "mpv.exe"),
        ]
        for cand in candidates:
            if os.path.isfile(cand):
                return cand
    return ""


def find_vlc(cfg: dict) -> str:
    configured = (cfg.get("vlc_path") or "").strip()
    if configured and os.path.isfile(configured):
        return configured
    exe = shutil.which("vlc") or shutil.which("vlc.exe")
    if exe:
        return exe
    candidates = []
    if os.name == "nt":
        candidates += [
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
        ]
    elif sys.platform == "darwin":
        candidates.append("/Applications/VLC.app/Contents/MacOS/VLC")
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return ""


def launch_player(exe: str, paths: list, extra_args: list | None = None) -> None:
    if not exe:
        raise FileNotFoundError("player not found")
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        [exe, *(extra_args or []), *paths],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        **kwargs,
    )


def mpv_extra_args(cfg: dict) -> list:
    args = []
    # Use SabMusic's own portable config instead of the user's ~/.config/mpv.
    if BUNDLED_MPV_CONFIG.is_dir():
        args.append(f"--config-dir={BUNDLED_MPV_CONFIG}")
    # Audio only: no video window and no embedded cover art / thumbnails.
    args.append("--no-video")
    args.append("--force-window=no")
    args.append("--audio-display=no")
    # Always normal speed (the portable config ships with speed=2).
    args.append("--speed=1")
    # Expose playback over MPRIS so desktop panels (quickshell, etc.) can
    # show the track and its pause/next/previous controls.
    mpris_plugin = Path("/usr/lib/mpv-mpris/mpris.so")
    if mpris_plugin.is_file():
        args.append(f"--script={mpris_plugin}")
    return args


def short_label(text, limit: int = 20) -> str:
    text = os.path.basename(text or "")
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def fmt_time(seconds) -> str:
    try:
        total = int(max(0.0, float(seconds)))
    except (TypeError, ValueError):
        return "0:00"
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def kill_stale_players() -> None:
    """Kill any leftover SabMusic mpv processes so only one can play at a time."""
    if os.name == "nt" or not os.path.isdir("/proc"):
        return
    marker = f"--config-dir={BUNDLED_MPV_CONFIG}".encode("utf-8", "surrogateescape")
    my_pid = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == my_pid:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read()
        except OSError:
            continue
        if b"mpv" in cmdline and marker in cmdline:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass


class MpvController:
    """Controls a headless mpv process over its JSON IPC socket."""

    def __init__(self):
        self.proc = None
        self.sock = None
        self.reader = None
        self.socket_path = None
        self.lock = threading.Lock()
        self.req_id = 0
        self.state = {}
        self.state_lock = threading.Lock()
        self.alive = False

    def active(self) -> bool:
        return self.sock is not None

    def start(self, exe: str, paths: list, base_args: list) -> None:
        self.stop()
        kill_stale_players()
        kwargs = {}
        if os.name == "nt":
            # JSON IPC over a Unix socket is not available on Windows here.
            kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
            self.proc = subprocess.Popen(
                [exe, *base_args, *paths],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, **kwargs,
            )
            return

        cache = _cache_dir()
        try:
            cache.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.socket_path = str(cache / f"mpv-{os.getpid()}-{int(time.time() * 1000)}.sock")
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except OSError:
            pass

        kwargs["start_new_session"] = True
        self.proc = subprocess.Popen(
            [exe, *base_args, f"--input-ipc-server={self.socket_path}", *paths],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, **kwargs,
        )
        self.alive = True
        threading.Thread(target=self._connect, daemon=True).start()

    def _connect(self) -> None:
        deadline = time.time() + 6
        while self.alive and time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(0.5)
                sock.connect(self.socket_path)
                sock.settimeout(0.3)
                self.sock = sock
                self.reader = sock.makefile("rb")
                threading.Thread(target=self._poll_loop, daemon=True).start()
                return
            except OSError:
                time.sleep(0.1)

    def _drop_socket(self) -> None:
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:
            pass
        self.sock = None
        self.reader = None

    def _request(self, command: list):
        with self.lock:
            if self.sock is None or self.reader is None:
                return None
            self.req_id += 1
            rid = self.req_id
            try:
                payload = json.dumps({"command": command, "request_id": rid})
                self.sock.sendall((payload + "\n").encode("utf-8"))
            except OSError:
                self._drop_socket()
                return None
            while True:
                try:
                    line = self.reader.readline()
                except (OSError, ValueError):
                    return None
                if not line:
                    self._drop_socket()
                    return None
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("request_id") == rid:
                    return msg

    def get(self, name: str):
        reply = self._request(["get_property", name])
        if reply and reply.get("error") == "success":
            return reply.get("data")
        return None

    def command(self, *args) -> None:
        self._request(list(args))

    def set_property(self, name: str, value) -> None:
        self._request(["set_property", name, value])

    def _poll_loop(self) -> None:
        props = ("filename", "pause", "time-pos", "duration", "volume",
                 "playlist-pos", "playlist-count", "idle-active")
        while self.alive and self.sock is not None:
            state = {}
            for prop in props:
                state[prop] = self.get(prop)
            with self.state_lock:
                self.state = state
            time.sleep(0.4)

    def snapshot(self) -> dict:
        with self.state_lock:
            return dict(self.state)

    def stop(self) -> None:
        self.alive = False
        self._drop_socket()
        proc = self.proc
        self.proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
            try:
                proc.wait(timeout=1)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except OSError:
                    pass
        if self.socket_path and os.name != "nt":
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
        self.socket_path = None
        with self.state_lock:
            self.state = {}


def kill_stale_vlc() -> None:
    """Kill leftover headless VLC instances started by SabMusic."""
    if os.name == "nt" or not os.path.isdir("/proc"):
        return
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmdline = fh.read()
        except OSError:
            continue
        if (b"vlc" in cmdline and b"--intf" in cmdline
                and b"dummy" in cmdline):
            try:
                os.kill(int(entry), signal.SIGTERM)
            except OSError:
                pass


class VlcController:
    """Controls a headless VLC process over MPRIS (via playerctl)."""

    FMT = ("{{status}}|{{title}}|{{position}}|{{mpris:length}}"
           "|{{volume}}|{{xesam:url}}|{{artist}}")

    def __init__(self):
        self.proc = None
        self.alive = False
        self.state = {}
        self.state_lock = threading.Lock()
        self.player = "vlc"
        self.initial_volume = 100.0
        self.shuffle = False

    def active(self) -> bool:
        return (self.alive and self.proc is not None
                and self.proc.poll() is None)

    def _playerctl(self, *args, timeout: float = 2.0):
        try:
            return subprocess.run(
                ["playerctl", f"--player={self.player}", *args],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, text=True, timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def start(self, exe: str, paths: list, base_args: list) -> None:
        self.stop()
        kill_stale_vlc()
        args = [exe, "--intf", "dummy", "--no-video", "--no-osd",
                "--quiet", "--no-one-instance",
                "--album-art-filename=__sabmusic_no_art__"]
        for path in paths:
            args.extend([path, f":meta-title={os.path.basename(path)}"])
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        self.alive = True
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _query(self) -> dict:
        res = self._playerctl("metadata", "--format", self.FMT)
        if res is None or res.returncode != 0:
            return {}
        parts = res.stdout.rstrip("\n").split("|")
        if len(parts) < 5:
            return {}
        status, title, pos, length, vol = parts[:5]
        url = parts[5] if len(parts) > 5 else ""
        try:
            position = float(pos or 0) / 1_000_000.0
        except ValueError:
            position = 0.0
        try:
            duration = float(length or 0) / 1_000_000.0
        except ValueError:
            duration = 0.0
        try:
            volume = float(vol or 0) * 100.0
        except ValueError:
            volume = 0.0
        if not title and url:
            title = url.rsplit("/", 1)[-1]
        path = ""
        if url:
            try:
                path = url2pathname(unquote(urlparse(url).path))
            except (TypeError, ValueError):
                path = ""
        return {
            "filename": title,
            "path": path,
            "pause": status == "Paused",
            "time-pos": position,
            "duration": duration,
            "volume": volume,
            "playlist-pos": 0,
            "playlist-count": 1,
            "idle-active": status == "Stopped",
        }

    def _poll_loop(self) -> None:
        first = True
        while self.alive and self.proc is not None and self.proc.poll() is None:
            state = self._query()
            with self.state_lock:
                self.state = state
            if first and state.get("filename"):
                # VLC's MPRIS volume often reports 0 initially; restore it.
                self.set_property("volume", self.initial_volume)
                if self.shuffle:
                    self.set_shuffle(True)
                first = False
            time.sleep(0.5)

    def get(self, name: str):
        return self.snapshot().get(name)

    def command(self, *args) -> None:
        if not args:
            return
        name = args[0]
        if name == "cycle" and len(args) > 1 and args[1] == "pause":
            self._playerctl("play-pause")
        elif name in ("playlist-next", "playlist_next", "next"):
            self._playerctl("next")
        elif name in ("playlist-prev", "playlist_prev", "previous"):
            self._playerctl("previous")
        elif name == "seek" and len(args) > 1:
            try:
                self._playerctl("position", f"{float(args[1]):.3f}")
            except (TypeError, ValueError):
                pass

    def set_shuffle(self, on: bool) -> None:
        self.shuffle = bool(on)
        self._playerctl("shuffle", "on" if self.shuffle else "off")

    def set_property(self, name: str, value) -> None:
        if name != "volume":
            return
        try:
            vol = max(0.0, min(1.0, float(value) / 100.0))
        except (TypeError, ValueError):
            return
        self._playerctl("volume", f"{vol:.3f}")

    def snapshot(self) -> dict:
        with self.state_lock:
            return dict(self.state)

    def stop(self) -> None:
        if self.alive:
            self._playerctl("stop", timeout=1.0)
        self.alive = False
        proc = self.proc
        self.proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except OSError:
                    pass
        with self.state_lock:
            self.state = {}


def reveal_in_folder(path: str) -> None:
    folder = os.path.dirname(path) or "."
    try:
        if os.name == "nt":
            os.startfile(folder)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except OSError:
        pass


class FoldersDialog(tk.Toplevel):
    def __init__(self, master, cfg: dict, on_save):
        super().__init__(master)
        self.title("Settings")
        self.cfg = cfg
        self.on_save = on_save
        self.transient(master)
        self.resizable(True, True)
        self.geometry("580x440")
        self.minsize(440, 360)
        self.configure(bg=BG)

        self.roots = list(cfg.get("roots") or [str(Path.home())])

        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Folders to scan for music files:").pack(anchor="w")
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True, pady=(6, 8))

        self.listbox = tk.Listbox(
            list_frame, selectmode="extended", height=10,
            bg=BG_FIELD, fg=FG, selectbackground=ACCENT,
            selectforeground=ACCENT_TEXT, highlightthickness=0,
            borderwidth=0, activestyle="none",
        )
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        btns = ttk.Frame(frame)
        btns.pack(fill="x")
        ttk.Button(btns, text="Add folder...", command=self._add).pack(side="left")
        ttk.Button(btns, text="Remove", command=self._remove).pack(side="left", padx=6)

        size_row = ttk.Frame(frame)
        size_row.pack(fill="x", pady=(8, 0))
        ttk.Label(size_row, text="Minimum file size (MB, 0 = no limit):").pack(side="left")
        self.min_size_var = tk.StringVar(
            value=str(cfg.get("min_size_mb", DEFAULT_MIN_MB))
        )
        ttk.Entry(size_row, textvariable=self.min_size_var, width=8).pack(
            side="left", padx=6
        )

        bottom = ttk.Frame(frame)
        bottom.pack(fill="x", pady=(10, 0))
        ttk.Button(bottom, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="Save", command=self._save).pack(side="right", padx=6)

        self._refresh()

    def _refresh(self):
        self.listbox.delete(0, "end")
        for root in self.roots:
            self.listbox.insert("end", root)

    def _add(self):
        chosen = filedialog.askdirectory(parent=self, title="Choose a folder to scan")
        if chosen:
            chosen = os.path.normpath(chosen)
            if chosen not in self.roots:
                self.roots.append(chosen)
                self._refresh()

    def _remove(self):
        for idx in reversed(self.listbox.curselection()):
            del self.roots[idx]
        self._refresh()

    def _save(self):
        if not self.roots:
            messagebox.showwarning(APP_NAME, "Add at least one folder.", parent=self)
            return
        try:
            min_mb = float(self.min_size_var.get().strip())
            if min_mb < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning(
                APP_NAME, "Minimum file size must be a non-negative number.",
                parent=self,
            )
            return
        self.cfg["roots"] = self.roots
        self.cfg["min_size_mb"] = min_mb
        save_config(self.cfg)
        self.on_save()
        self.destroy()


class SabMusicApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1040x640")
        self.minsize(680, 420)

        self.cfg = load_config()
        self.mpv = find_mpv(self.cfg)
        self.vlc = find_vlc(self.cfg)
        self.items = []           # all discovered tracks
        self.shown_items = []     # currently displayed (filtered/sorted) tracks
        self.sort_col = "added"   # "Recently added" is the default view
        self.sort_reverse = True
        self.scanning = False
        self._scan_q = queue.Queue()
        self.player = VlcController()
        self.shuffle_on = False
        self.most_periods = ("nothing", "all time", "last week", "last month")
        self.most_period_idx = 0
        self.current_paths = []
        self.current_index = 0
        self.current_path = None
        self.current_row = None
        self.seek_dragging = False
        self.vol_dragging = False
        self._build_styles()
        self._build_ui()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        cached = load_cache()
        if cached:
            self.items = cached
            self._apply_filter()

        self.after(150, self._poll_scan)
        self.after(200, self._poll_player)
        if self.cfg.get("scan_on_startup", True):
            self.after(300, self.start_scan)
        else:
            self._update_status()

    # ---------- UI ----------
    def _build_styles(self):
        self.configure(bg=BG)
        try:
            self.tk_setPalette(
                background=BG, foreground=FG,
                activeBackground=BG_HOVER, activeForeground=FG,
                highlightBackground=BG, highlightColor=ACCENT,
                selectBackground=ACCENT, selectForeground=ACCENT_TEXT,
                insertBackground=FG,
            )
        except tk.TclError:
            pass
        self.option_add("*TCombobox*Listbox.background", BG_FIELD)
        self.option_add("*TCombobox*Listbox.foreground", FG)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.option_add("*TCombobox*Listbox.selectForeground", ACCENT_TEXT)

        style = ttk.Style(self)
        try:
            if "clam" in style.theme_names():
                style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(".", background=BG, foreground=FG,
                        fieldbackground=BG_FIELD, bordercolor=BORDER,
                        lightcolor=BG_PANEL, darkcolor=BG_PANEL)

        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Toolbar.TFrame", background=BG_PANEL, padding=8)
        style.configure("Toolbar.TLabel", background=BG_PANEL, foreground=FG)
        style.configure("Muted.TLabel", background=BG_PANEL, foreground=FG_MUTED)
        style.configure("Status.TLabel", background=BG_PANEL, foreground=FG_MUTED,
                        padding=(8, 4))
        style.configure("Player.TFrame", background=BG_PANEL)
        style.configure("Player.TLabel", background=BG_PANEL, foreground=FG)
        style.configure("PlayerMuted.TLabel", background=BG_PANEL, foreground=FG_MUTED)
        style.configure("Player.TButton", background=BG_HOVER, foreground=FG,
                        bordercolor=BORDER, padding=(8, 3))
        style.map("Player.TButton",
                  background=[("pressed", BG_FIELD), ("active", ACCENT)],
                  foreground=[("active", ACCENT_TEXT)])
        style.configure("Player.Horizontal.TScale", background=BG_PANEL,
                        troughcolor=BG_FIELD, bordercolor=BORDER,
                        lightcolor=ACCENT, darkcolor=ACCENT)

        style.configure(
            "TButton", background=BG_PANEL, foreground=FG,
            bordercolor=BORDER, focuscolor=ACCENT,
            lightcolor=BG_PANEL, darkcolor=BG_PANEL,
            padding=(10, 4),
        )
        style.map(
            "TButton",
            background=[("pressed", BG_HOVER), ("active", BG_HOVER)],
            foreground=[("disabled", FG_MUTED)],
            bordercolor=[("focus", ACCENT)],
        )
        style.configure("Danger.TButton", background=BG_PANEL, foreground=DANGER,
                        bordercolor=BORDER)
        style.map("Danger.TButton",
                  background=[("pressed", BG_HOVER), ("active", BG_HOVER)],
                  foreground=[("disabled", FG_MUTED)])

        style.configure(
            "TEntry", fieldbackground=BG_FIELD, foreground=FG,
            insertcolor=FG, bordercolor=BORDER, lightcolor=BORDER,
            darkcolor=BORDER, padding=4,
        )
        style.map("TEntry",
                  bordercolor=[("focus", ACCENT)],
                  fieldbackground=[("disabled", BG_PANEL)])

        style.configure(
            "Treeview",
            background=BG, fieldbackground=BG, foreground=FG,
            bordercolor=BG, lightcolor=BG, darkcolor=BG, rowheight=28,
        )
        style.map(
            "Treeview",
            background=[("selected", ACCENT)],
            foreground=[("selected", ACCENT_TEXT)],
        )
        style.configure(
            "Treeview.Heading",
            background=BG_PANEL, foreground=FG, relief="flat",
            bordercolor=BORDER, font=("TkDefaultFont", 10, "bold"),
            padding=(6, 5),
        )
        style.map("Treeview.Heading",
                  background=[("active", BG_HOVER)],
                  foreground=[("active", FG)])

        style.configure(
            "TScrollbar", background=BG_PANEL, troughcolor=BG,
            bordercolor=BG, arrowcolor=FG_MUTED, lightcolor=BG_PANEL,
            darkcolor=BG_PANEL,
        )
        style.map("TScrollbar",
                  background=[("active", BG_HOVER), ("pressed", BG_HOVER)])

        style.configure(
            "TCombobox", fieldbackground=BG_FIELD, foreground=FG,
            background=BG_PANEL, bordercolor=BORDER, lightcolor=BORDER,
            darkcolor=BORDER, arrowcolor=FG, insertcolor=FG,
        )
        style.map("TCombobox",
                  fieldbackground=[("readonly", BG_FIELD), ("disabled", BG_PANEL)],
                  foreground=[("readonly", FG), ("disabled", FG_MUTED)],
                  selectbackground=[("readonly", BG_FIELD)],
                  selectforeground=[("readonly", FG)])

    def _build_ui(self):
        toolbar = ttk.Frame(self, style="Toolbar.TFrame")
        toolbar.pack(fill="x")
        self._build_player_bar()

        ttk.Label(toolbar, text="Search:", style="Toolbar.TLabel").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        self.search_entry = ttk.Entry(toolbar, textvariable=self.search_var, width=34)
        self.search_entry.pack(side="left", padx=(6, 12))

        ttk.Button(toolbar, text="Rescan", command=self.start_scan).pack(side="left")
        ttk.Button(toolbar, text="Settings...", command=self._open_settings).pack(side="left", padx=6)
        ttk.Button(toolbar, text="Reload", command=self._reload_app).pack(side="left", padx=6)

        ttk.Label(toolbar, text="Player:", style="Toolbar.TLabel").pack(side="left", padx=(12, 4))
        self.player_var = tk.StringVar(
            value=self.cfg.get("player") if self.cfg.get("player") in ("mpv", "vlc") else "mpv"
        )
        player_box = ttk.Combobox(
            toolbar, textvariable=self.player_var, state="readonly",
            values=("mpv", "vlc"), width=6,
        )
        player_box.pack(side="left")
        player_box.bind("<<ComboboxSelected>>", lambda _e: self._save_player_choice())
        ttk.Button(toolbar, text="Play", command=self.play_selected).pack(side="left", padx=(12, 0))

        self.count_var = tk.StringVar(value="")
        ttk.Label(toolbar, textvariable=self.count_var,
                  style="Muted.TLabel").pack(side="right")

        columns = ("added", "size")
        self.tree = ttk.Treeview(
            self, columns=columns, show="tree headings", selectmode="extended",
        )
        self.tree.heading("#0", text="Filename", command=lambda: self._sort_by("name"))
        self.tree.heading("added", text="Recently added", command=lambda: self._sort_by("added"))
        self.tree.heading("size", text="Size", command=lambda: self._sort_by("size"))
        self.tree.column("#0", width=210, minwidth=160, stretch=False)
        self.tree.column("added", width=220, minwidth=120, stretch=True, anchor="w")
        self.tree.column("size", width=90, minwidth=70, stretch=False, anchor="e")

        self.tree.tag_configure("track", foreground=FG)
        self.tree.tag_configure("alt", background=ROW_ALT)
        self.tree.tag_configure("current", background=ACCENT,
                                foreground=ACCENT_TEXT)

        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self.tree.bind("<Return>", self._on_tree_activate)

        self.menu = tk.Menu(self, tearoff=0, bg=BG_PANEL, fg=FG,
                            activebackground=ACCENT, activeforeground=ACCENT_TEXT,
                            bd=1, relief="solid")
        self.menu.add_command(label="Play", command=self.play_selected)
        self.menu.add_command(label="Play next", command=self.play_next_selected)
        self.menu.add_command(label="Play with mpv", command=self.play_selected_mpv)
        self.menu.add_command(label="Play with VLC", command=self.play_selected_vlc)
        self.menu.add_command(label="Open containing folder", command=self._open_folder_selected)
        self.menu.add_command(label="Copy path", command=self._copy_path)
        self.menu.add_separator()
        self.menu.add_command(label="Delete permanently...", command=self.delete_selected)
        self.tree.bind("<Button-3>", self._show_menu)
        if sys.platform == "darwin":
            self.tree.bind("<Button-2>", self._show_menu)

        self.status_var = tk.StringVar(value="Ready")
        status = ttk.Label(self, textvariable=self.status_var, style="Status.TLabel",
                           anchor="w")
        status.pack(side="bottom", fill="x")

    def _build_player_bar(self):
        bar = ttk.Frame(self, style="Player.TFrame")
        bar.pack(side="top", fill="x")

        self.now_var = tk.StringVar(value="Nothing playing")
        now = ttk.Label(bar, textvariable=self.now_var, style="Player.TLabel",
                        anchor="w", font=("TkDefaultFont", 10, "bold"))
        now.pack(side="top", fill="x", padx=10, pady=(6, 2))

        row = ttk.Frame(bar, style="Player.TFrame")
        row.pack(side="top", fill="x", padx=10, pady=(0, 6))

        self.prev_btn = ttk.Button(row, text="Prev", style="Player.TButton",
                                   width=5, command=self.player_prev)
        self.play_btn = ttk.Button(row, text="Play", style="Player.TButton",
                                   width=6, command=self.player_play_pause)
        self.next_btn = ttk.Button(row, text="Next", style="Player.TButton",
                                   width=5, command=self.player_next)
        self.stop_btn = ttk.Button(row, text="Stop", style="Player.TButton",
                                   width=5, command=self.player_stop)
        self.shuffle_btn = ttk.Button(row, text="Shuffle: off", style="Player.TButton",
                                      width=11, command=self.toggle_shuffle)
        self.most_played_btn = ttk.Button(row, text="Top: nothing",
                                          style="Player.TButton", width=13,
                                          command=self.play_most_played)
        self.prev_btn.pack(side="left")
        self.play_btn.pack(side="left", padx=4)
        self.next_btn.pack(side="left")
        self.stop_btn.pack(side="left", padx=(4, 4))
        self.shuffle_btn.pack(side="left")
        self.most_played_btn.pack(side="left", padx=(4, 0))

        self.time_var = tk.StringVar(value="0:00 / 0:00")
        ttk.Label(row, textvariable=self.time_var, style="PlayerMuted.TLabel",
                  width=15).pack(side="left")

        self.seek_var = tk.DoubleVar(value=0)
        self.seek = ttk.Scale(row, from_=0, to=1000, variable=self.seek_var,
                              orient="horizontal", style="Player.Horizontal.TScale")
        self.seek.pack(side="left", fill="x", expand=True, padx=8)
        self.seek.bind("<ButtonPress-1>", lambda _e: setattr(self, "seek_dragging", True))
        self.seek.bind("<ButtonRelease-1>", self._seek_released)

        ttk.Label(row, text="Vol", style="PlayerMuted.TLabel").pack(side="left", padx=(8, 2))
        self.vol_var = tk.DoubleVar(value=100)
        self.vol = ttk.Scale(row, from_=0, to=100, variable=self.vol_var,
                             orient="horizontal", style="Player.Horizontal.TScale",
                             length=90)
        self.vol.pack(side="left")
        self.vol.bind("<ButtonPress-1>", lambda _e: setattr(self, "vol_dragging", True))
        self.vol.bind("<ButtonRelease-1>", self._volume_released)

    def _bind_keys(self):
        self.bind("<Control-f>", lambda _e: self.search_entry.focus_set())
        self.bind("<Escape>", lambda _e: self.search_var.set(""))
        self.tree.bind("<Delete>", lambda _e: self.delete_selected())
        self.bind("<F5>", lambda _e: self.start_scan())

    def _show_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.menu.tk_popup(event.x_root, event.y_root)

    # ---------- scanning ----------
    def start_scan(self):
        if self.scanning:
            return
        self.scanning = True
        self.status_var.set("Scanning for music...")
        roots = self.cfg.get("roots") or [str(Path.home())]
        min_bytes = int(float(self.cfg.get("min_size_mb", DEFAULT_MIN_MB)) * 1024 * 1024)
        threading.Thread(target=self._scan_worker, args=(roots, min_bytes),
                         daemon=True).start()

    def _scan_worker(self, roots, min_bytes):
        found = []
        seen = set()
        try:
            for root in roots:
                root = os.path.expanduser(str(root))
                if not os.path.isdir(root):
                    continue
                for dirpath, dirnames, filenames in os.walk(
                        root, onerror=lambda _e: None, followlinks=False):
                    dirnames[:] = [
                        d for d in dirnames
                        if not d.startswith(".") and d.lower() not in SKIP_DIR_NAMES
                    ]
                    for fname in filenames:
                        if os.path.splitext(fname)[1].lower() not in AUDIO_EXTS:
                            continue
                        full = os.path.join(dirpath, fname)
                        if full in seen:
                            continue
                        try:
                            st = os.stat(full)
                        except OSError:
                            continue
                        if st.st_size < min_bytes:
                            continue
                        seen.add(full)
                        found.append({
                            "path": full,
                            "name": fname,
                            "folder": dirpath,
                            "size": st.st_size,
                            "mtime": st.st_mtime,
                        })
                        if len(found) % 100 == 0:
                            self._scan_q.put(("progress", len(found)))
        except Exception as exc:  # noqa: BLE001
            self._scan_q.put(("error", str(exc)))
        self._scan_q.put(("done", found))

    def _poll_scan(self):
        try:
            while True:
                kind, payload = self._scan_q.get_nowait()
                if kind == "progress":
                    self.status_var.set(f"Scanning... {payload} tracks found")
                elif kind == "done":
                    self._finish_scan(payload)
                elif kind == "error":
                    self.scanning = False
                    self.status_var.set(f"Scan error: {payload}")
        except queue.Empty:
            pass
        self.after(150, self._poll_scan)

    def _finish_scan(self, found):
        self.scanning = False
        self.items = found
        save_cache(self.items)
        self._apply_filter()
        self.status_var.set(
            f"Found {len(self.items)} track(s) "
            f"(min {self.cfg.get('min_size_mb', DEFAULT_MIN_MB)} MB)"
        )

    # ---------- filtering / sorting ----------
    def _apply_filter(self):
        query = self.search_var.get().strip().lower()
        words = query.split()
        shown = []
        for item in self.items:
            haystack = f'{item.get("name", "")} {item.get("path", "")}'.lower()
            if all(w in haystack for w in words):
                shown.append(item)

        if self.sort_col == "added":
            shown.sort(key=lambda i: float(i.get("mtime") or 0),
                       reverse=self.sort_reverse)
        elif self.sort_col == "size":
            shown.sort(key=lambda i: int(i.get("size") or 0),
                       reverse=self.sort_reverse)
        else:
            shown.sort(key=lambda i: natural_key(i.get("name", "")),
                       reverse=self.sort_reverse)

        self.shown_items = shown
        self._populate_tree()
        self._update_status()

    def _populate_tree(self):
        self.tree.delete(*self.tree.get_children())
        for idx, item in enumerate(self.shown_items):
            iid = f"t{idx}"
            stripe = ("alt",) if idx % 2 else ()
            self.tree.insert(
                "", "end", iid=iid, text=short_label(item.get("name", ""), limit=20),
                values=(added_label(item.get("mtime")),
                        human_size(int(item.get("size", 0)))),
                tags=stripe + ("track",),
            )
        self._highlight_current_row()

    def _highlight_current_row(self):
        old = self.current_row
        if old and self.tree.exists(old):
            idx = int(old[1:]) if old[1:].isdigit() else 0
            stripe = ("alt",) if idx % 2 else ()
            self.tree.item(old, tags=stripe + ("track",))
        self.current_row = None
        path = self.current_path
        if not path:
            return
        for idx, item in enumerate(self.shown_items):
            if item.get("path") == path:
                iid = f"t{idx}"
                if self.tree.exists(iid):
                    self.tree.item(iid, tags=("current",))
                    self.current_row = iid
                    if self.tree.bbox(iid) is None:
                        self.tree.see(iid)
                break

    def _on_tree_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return "break"
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.play_selected()
        return "break"

    def _on_tree_activate(self, _event=None):
        self.play_selected()

    def _sort_by(self, col):
        if self.sort_col == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = col
            self.sort_reverse = col in ("added", "size")
        self._apply_filter()

    def _update_status(self):
        total = len(self.items)
        self.count_var.set(f"{len(self.shown_items)} / {total} shown")
        if not self.scanning and self.items:
            self.status_var.set(
                f"{total} track(s) · sorted by "
                f"{'size' if self.sort_col == 'size' else 'name' if self.sort_col == 'name' else 'recently added'}"
                f" · min {self.cfg.get('min_size_mb', DEFAULT_MIN_MB)} MB"
            )

    # ---------- selection helpers ----------
    def _selected_items(self):
        selected = set(self.tree.selection())
        result = []
        for idx, item in enumerate(self.shown_items):
            if f"t{idx}" in selected:
                result.append(item)
        return result

    # ---------- actions ----------
    def play_selected(self):
        self.play_selected_mpv()

    def play_selected_mpv(self):
        if not self.vlc:
            messagebox.showerror(APP_NAME, "VLC is not installed.", parent=self)
            return
        selected = self._selected_items()
        if not selected:
            return
        first = selected[0].get("path")
        if not first or not os.path.isfile(first):
            messagebox.showerror(
                APP_NAME,
                f"File no longer exists:\n{first or selected[0].get('name', '')}",
                parent=self,
            )
            return
        paths = [p for p in self._playlist_paths(selected)
                 if p and os.path.isfile(p)]
        if not paths:
            messagebox.showerror(APP_NAME, "File no longer exists.", parent=self)
            return
        try:
            self.player.initial_volume = float(self.vol_var.get())
            self.player.shuffle = self.shuffle_on
            self.player.start(self.vlc, paths, [])
            self.current_paths = paths
            self.current_index = 0
            self._notify_now_playing(paths[0])
            self.now_var.set("▶ " + short_label(paths[0]))
            self.play_btn.configure(text="Pause")
            if len(paths) > 1:
                self.status_var.set(
                    f"Playing {len(paths)} track(s): {os.path.basename(paths[0])}"
                )
            else:
                self.status_var.set(f"Playing: {os.path.basename(paths[0])}")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not launch VLC:\n{exc}", parent=self)

    def play_selected_vlc(self):
        self.play_selected_mpv()

    def _notify_now_playing(self, path):
        name = os.path.basename(path) or path
        try:
            subprocess.Popen(
                ["notify-send", "-a", APP_NAME, "-t", "5000", name, "Now playing"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    def play_next_selected(self):
        selected = self._selected_items()
        if not selected:
            return
        next_paths = [i.get("path") for i in selected
                      if i.get("path") and os.path.isfile(i.get("path"))]
        if not next_paths:
            messagebox.showerror(APP_NAME, "File no longer exists.", parent=self)
            return
        if not self.player.active() or not self.current_paths:
            self.play_selected_mpv()
            return
        snap = self.player.snapshot()
        try:
            pos = float(snap.get("time-pos") or 0)
        except (TypeError, ValueError):
            pos = 0.0
        current = self.current_paths[self.current_index]
        rest = [p for p in self.current_paths[self.current_index + 1:]
                if p not in next_paths]
        new_paths = [current, *next_paths, *rest]
        self.current_paths = new_paths
        self.current_index = 0
        try:
            self.player.initial_volume = float(self.vol_var.get())
            self.player.shuffle = self.shuffle_on
            self.player.start(self.vlc, new_paths, [])
            self.after(1200, lambda: self.player.command("seek", pos, "absolute"))
            self.now_var.set("▶ " + short_label(current))
            self.status_var.set(
                "Playing next: " + ", ".join(short_label(p) for p in next_paths)
            )
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not launch VLC:\n{exc}", parent=self)

    def toggle_shuffle(self):
        self.shuffle_on = not self.shuffle_on
        self.shuffle_btn.configure(
            text="Shuffle: on" if self.shuffle_on else "Shuffle: off"
        )
        self.player.set_shuffle(self.shuffle_on)
        self.status_var.set("Shuffle on" if self.shuffle_on else "Shuffle off")

    def _record_play(self, path):
        plays = load_plays()
        times = plays.get(path)
        if not isinstance(times, list):
            times = []
        now = time.time()
        if times:
            try:
                if now - float(times[-1]) < 2:
                    return
            except (TypeError, ValueError):
                pass
        times.append(now)
        plays[path] = times[-1000:]
        save_plays(plays)

    def _start_top_playlist(self, paths, status):
        if not paths:
            self.status_var.set("No tracks available")
            return
        if self.shuffle_on:
            self.shuffle_on = False
            self.shuffle_btn.configure(text="Shuffle: off")
        try:
            self.player.initial_volume = float(self.vol_var.get())
            self.player.shuffle = False
            self.player.start(self.vlc, paths, [])
            self.current_paths = paths
            self.current_index = 0
            self._notify_now_playing(paths[0])
            self.now_var.set("▶ " + short_label(paths[0]))
            self.play_btn.configure(text="Pause")
            self.status_var.set(status)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not launch VLC:\n{exc}", parent=self)

    def play_most_played(self):
        period = self.most_periods[self.most_period_idx]
        self.most_period_idx = (self.most_period_idx + 1) % len(self.most_periods)
        self.most_played_btn.configure(
            text="Top: " + self.most_periods[self.most_period_idx]
        )
        if not self.vlc:
            messagebox.showerror(APP_NAME, "VLC is not installed.", parent=self)
            return
        if period == "nothing":
            tracks = [x for x in self.items
                      if x.get("path") and os.path.isfile(x.get("path"))]
            tracks.sort(key=lambda x: float(x.get("mtime") or 0), reverse=True)
            self._start_top_playlist(
                [x.get("path") for x in tracks],
                f"Recently added: {len(tracks)} track(s)",
            )
            return
        plays = load_plays()
        now = time.time()
        window = {"last week": 7 * 86400, "last month": 30 * 86400}.get(period)
        scored = []
        for item in self.items:
            path = item.get("path")
            times = plays.get(path)
            if not isinstance(times, list):
                continue
            try:
                count = sum(1 for t in times
                            if window is None or now - float(t) <= window)
            except (TypeError, ValueError):
                continue
            if count > 0:
                scored.append((count, item))
        if not scored:
            self.status_var.set(f"No plays in {period} yet")
            return
        scored.sort(key=lambda pair: (-pair[0],
                                      short_label(pair[1].get("name", ""))))
        paths = [item.get("path") for _, item in scored
                 if item.get("path") and os.path.isfile(item.get("path"))]
        self._start_top_playlist(
            paths, f"Most played ({period}): {len(paths)} track(s)"
        )

    def _playlist_paths(self, selected):
        if not selected:
            return []
        selected_paths = {item.get("path") for item in selected}
        ordered = [item for item in self.shown_items
                   if item.get("path") in selected_paths]
        if len(ordered) > 1:
            return [item.get("path") for item in ordered if item.get("path")]
        # Just one track chosen: continue playing the rest of the list.
        first = ordered[0] if ordered else selected[0]
        try:
            start = self.shown_items.index(first)
        except ValueError:
            start = 0
        return [item.get("path") for item in self.shown_items[start:]
                if item.get("path")]

    def _play(self, player, name, warn, extra_args=None):
        selected = self._selected_items()
        if not selected:
            return
        if not player:
            warn()
            return
        first = selected[0].get("path")
        if not first or not os.path.isfile(first):
            messagebox.showerror(
                APP_NAME,
                f"File no longer exists:\n{first or selected[0].get('name', '')}",
                parent=self,
            )
            return
        paths = [p for p in self._playlist_paths(selected)
                 if p and os.path.isfile(p)]
        if not paths:
            messagebox.showerror(APP_NAME, "File no longer exists.", parent=self)
            return
        try:
            launch_player(player, paths, extra_args)
            if len(paths) > 1:
                self.status_var.set(
                    f"Playing {len(paths)} track(s) with {name}: "
                    f"{os.path.basename(paths[0])}"
                )
            else:
                self.status_var.set(
                    f"Playing with {name}: {os.path.basename(paths[0])}"
                )
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not launch {name}:\n{exc}", parent=self)

    # ---------- playback controls ----------
    def player_play_pause(self):
        if self.player.active():
            self.player.command("cycle", "pause")

    def player_next(self):
        if self.player.active():
            self.player.command("playlist-next")

    def player_prev(self):
        if self.player.active():
            self.player.command("playlist-prev")

    def player_stop(self):
        if self.player.active():
            self.player.stop()
        self.now_var.set("Nothing playing")
        self.play_btn.configure(text="Play")
        self.seek_var.set(0)
        self.time_var.set("0:00 / 0:00")
        self.status_var.set("Stopped")
        self.current_path = None
        self._highlight_current_row()

    def _seek_released(self, _event=None):
        self.seek_dragging = False
        if not self.player.active():
            return
        try:
            duration = float(self.player.snapshot().get("duration"))
        except (TypeError, ValueError):
            return
        if duration <= 0:
            return
        self.player.command("seek", (self.seek_var.get() / 1000.0) * duration, "absolute")

    def _volume_released(self, _event=None):
        self.vol_dragging = False
        if self.player.active():
            self.player.set_property("volume", float(self.vol_var.get()))

    def _poll_player(self):
        snap = self.player.snapshot()
        if snap:
            title = snap.get("filename")
            if title:
                paused = bool(snap.get("pause"))
                self.now_var.set(("⏸ " if paused else "▶ ") + short_label(str(title)))
                self.play_btn.configure(text="Play" if paused else "Pause")
            path = snap.get("path")
            if path and path != self.current_path:
                self.current_path = path
                self._record_play(path)
                self._highlight_current_row()
            try:
                duration = float(snap.get("duration") or 0)
                position = float(snap.get("time-pos") or 0)
            except (TypeError, ValueError):
                duration = position = 0.0
            if duration > 0:
                if not self.seek_dragging:
                    self.seek_var.set(max(0.0, min(1000.0, position / duration * 1000.0)))
                self.time_var.set(f"{fmt_time(position)} / {fmt_time(duration)}")
            try:
                volume = float(snap.get("volume"))
                if not self.vol_dragging:
                    self.vol_var.set(max(0.0, min(100.0, volume)))
            except (TypeError, ValueError):
                pass
        elif self.current_path:
            self.current_path = None
            self._highlight_current_row()
        self.after(400, self._poll_player)

    def _reload_app(self):
        self.player.stop()
        script = os.path.abspath(__file__)
        python = sys.executable or "python3"
        try:
            os.execv(python, [python, script, *sys.argv[1:]])
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not reload:\n{exc}", parent=self)

    def _on_close(self):
        try:
            self.player.stop()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()

    def delete_selected(self):
        selected = self._selected_items()
        if not selected:
            messagebox.showinfo(APP_NAME, "Select one or more tracks first.", parent=self)
            return

        if len(selected) == 1:
            item = selected[0]
            detail = f"{item['name']}\n\n{item['path']}"
        else:
            detail = f"{len(selected)} selected files:\n\n" + "\n".join(
                f"- {item['name']} ({human_size(int(item['size']))})"
                for item in selected[:12]
            )
            if len(selected) > 12:
                detail += f"\n... and {len(selected) - 12} more"

        confirmed = messagebox.askyesno(
            "Delete permanently?",
            f"This will PERMANENTLY delete:\n\n{detail}\n\n"
            "This cannot be undone. Continue?",
            icon="warning",
            default="no",
            parent=self,
        )
        if not confirmed:
            return

        errors = []
        deleted_paths = set()
        for item in selected:
            try:
                os.remove(item["path"])
                deleted_paths.add(item["path"])
            except FileNotFoundError:
                deleted_paths.add(item["path"])
            except OSError as exc:
                errors.append(f"{item['name']}: {exc}")

        if deleted_paths:
            self.items = [i for i in self.items if i["path"] not in deleted_paths]
            save_cache(self.items)
            self._apply_filter()

        if errors:
            messagebox.showerror(
                APP_NAME,
                "Some files could not be deleted:\n\n" + "\n".join(errors[:10]),
                parent=self,
            )
        else:
            self.status_var.set(f"Permanently deleted {len(deleted_paths)} file(s)")

    def _open_folder_selected(self):
        selected = self._selected_items()
        if selected:
            reveal_in_folder(selected[0]["path"])

    def _copy_path(self):
        selected = self._selected_items()
        if selected:
            self.clipboard_clear()
            self.clipboard_append("\n".join(item["path"] for item in selected))
            self.status_var.set("Path copied to clipboard")

    def _save_player_choice(self):
        self.cfg["player"] = self.player_var.get()
        save_config(self.cfg)

    def _open_settings(self):
        FoldersDialog(self, self.cfg,
                      on_save=lambda: (self._update_status(), self.start_scan()))

    def _warn_no_vlc(self):
        messagebox.showwarning(
            APP_NAME,
            "VLC was not found.\n\n"
            "Install VLC and make sure it is on your PATH, or set its full path "
            f"in:\n{CONFIG_FILE}\n\n(\"vlc_path\": \"...\")",
            parent=self,
        )


def _acquire_single_instance():
    """Hold an exclusive lock so only one SabMusic can run (and thus play)."""
    if os.name == "nt":
        return True
    try:
        import fcntl
    except ImportError:
        return True
    lock_path = _cache_dir() / "sabmusic.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "w")
    except OSError:
        return True
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle

# if nothing found show filename in notification panel 
# right click play next option 
# if possible try to not show image in norification panel ( not mandatory )
# shuffle button 

def main():
    lock = _acquire_single_instance()
    if lock is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            APP_NAME,
            "SabMusic is already running.\n\n"
            "Only one instance can play music at a time. Use the Reload "
            "button in the running window to restart it with updated code.",
        )
        root.destroy()
        return
    app = SabMusicApp()
    app.mainloop()


if __name__ == "__main__":
    main()
