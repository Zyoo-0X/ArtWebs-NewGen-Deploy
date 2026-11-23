from __future__ import annotations

import argparse
import atexit
import filecmp
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import queue
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
GAMES = PROJECT / "games"
BUILD = PROJECT / "build"
TOOLS = PROJECT / "tools"
REMOTE_WORKTREE = PROJECT / ".github"
CFG = PROJECT / ".games.json"
ENV_FILE = PROJECT / ".env"
LOG_DIR = PROJECT / "logs"
LOG_FILE = LOG_DIR / "games.log"
SESSION_FILE = PROJECT / "games.save"
PFS_RELEASE_API = "https://api.github.com/repos/sakarie9/pfs-rs/releases/latest"
MB = 1024 * 1024
DEFAULT_MAX_FILE_MB = 90
DEFAULT_BATCH_PUSH_MB = 250
DEBUG = False
PFS_RE = re.compile(r"^(?P<stem>.+)\.(?P<ext>pfs|pf6|pf8)(?:\.(?P<volume>\d{3}))?$", re.IGNORECASE)


@dataclass
class GameItem:
    name: str
    root: Path
    files: list[Path]
    loose: bool = False

    @property
    def size(self) -> int:
        return sum(p.stat().st_size for p in self.files if p.is_file())


@dataclass
class PreparedGame:
    item: GameItem
    slug: str
    output: Path
    file_count: int
    total_bytes: int


def set_debug(enabled: bool) -> None:
    global DEBUG
    DEBUG = bool(enabled)


def log(level: str, message: str) -> None:
    text = mask_secret(str(message))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.is_file() and LOG_FILE.stat().st_size > 5 * MB:
            LOG_FILE.replace(LOG_FILE.with_suffix(".old.log"))
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} [{level}] {text}\n")
    except OSError:
        pass
    if DEBUG and level != "PROGRESS":
        shown = text if len(text) <= 400 else text[:400] + "... (truncated, full text in log file)"
        if sys.stdout.isatty() and not ProgressBar._bar_on:
            sys.stdout.write("\r" + " " * (shutil.get_terminal_size((120, 20)).columns - 1) + "\r")
        print(f"  [{level}] {shown}")


def log_file(action: str, path: str, size: int | None = None, counter: str = "") -> None:
    if not DEBUG:
        return
    prefix = f"[{counter}] " if counter else ""
    detail = f" ({fmt(size)})" if size is not None else ""
    log("FILE", f"{prefix}{action:<8} {path}{detail}")


def fmt_time(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def clear_screen() -> None:
    ProgressBar.release()
    if sys.stdout.isatty():
        os.system("cls" if os.name == "nt" else "clear")
    else:
        print("\n" + "=" * 60)


def show_header(*path: str) -> None:
    clear_screen()
    show_banner()
    log("COMMAND", " > ".join(path))


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"\n{prompt} ({hint}): ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("Type y or n")


PAUSE_SECONDS = 10


def pause(seconds: int = PAUSE_SECONDS) -> None:
    ProgressBar.release()
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        return
    skip_keys = ("\r", "\n", " ")
    end = time.monotonic() + seconds
    shown = -1

    def draw() -> None:
        nonlocal shown
        left = max(int(end - time.monotonic() + 0.999), 0)
        if left != shown:
            shown = left
            sys.stdout.write(f"\r  Back in {left}s (Enter/Space = now) ")
            sys.stdout.flush()
    try:
        if os.name == "nt":
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getwch()
            while time.monotonic() < end:
                draw()
                if msvcrt.kbhit() and msvcrt.getwch() in skip_keys:
                    break
                time.sleep(0.05)
        else:
            import select
            import termios
            import tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                termios.tcflush(fd, termios.TCIFLUSH)
                while time.monotonic() < end:
                    draw()
                    if select.select([sys.stdin], [], [], 0.1)[0] and sys.stdin.read(1) in skip_keys:
                        break
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except KeyboardInterrupt:
        pass
    sys.stdout.write("\r" + " " * 40 + "\r")
    sys.stdout.flush()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Session:
    def __init__(self) -> None:
        self.active = False
        self.data: dict = {}

    def start(self, repo: str, branch: str, max_file_mb: int, batch_push_mb: int, games: list[str], resumed: bool = False) -> None:
        now = _now()
        self.data = {
            "version": 1,
            "status": "running",
            "pid": os.getpid(),
            "started_at": now,
            "updated_at": now,
            "resumed": resumed,
            "repo": repo,
            "branch": branch,
            "max_file_mb": max_file_mb,
            "batch_push_mb": batch_push_mb,
            "games": games,
            "phase": "starting",
            "current_game": "",
            "detail": "",
            "batch": 0,
            "batches": 0,
            "batches_done": 0,
            "prepared": [],
            "skipped": [],
            "error": "",
            "push_record": "",
            "log_file": str(LOG_FILE),
        }
        self.active = True
        self.save()
        log("SESSION", f"started, checkpoint: {SESSION_FILE} (resumed={resumed})")

    def update(self, **fields) -> None:
        if not self.active:
            return
        self.data.update(fields)
        self.data["updated_at"] = _now()
        self.save()

    def save(self) -> None:
        tmp = SESSION_FILE.parent / (SESSION_FILE.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, SESSION_FILE)
        except OSError as exc:
            log("WARN", f"could not write {SESSION_FILE}: {exc}")

    def interrupt(self, status: str, error: str = "") -> None:
        if self.active and self.data.get("status") == "running":
            self.update(status=status, error=(error or self.data.get("error", ""))[:500])
            log("SESSION", f"saved as '{status}': {error}")

    def finish(self) -> None:
        self.active = False
        self.delete()
        log("SESSION", "finished cleanly, checkpoint removed")

    @staticmethod
    def load() -> dict | None:
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def delete() -> None:
        try:
            SESSION_FILE.unlink(missing_ok=True)
        except OSError:
            pass


SESSION = Session()


def _install_exit_hooks() -> None:
    def on_signal(signum, _frame):
        SESSION.interrupt("closed", f"window closed or process stopped (signal {signum})")
        log("EXIT", f"signal {signum}")
        raise SystemExit(130)

    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, on_signal)
            except (OSError, ValueError):
                pass
    atexit.register(lambda: SESSION.interrupt("closed", "program exited before the upload finished"))


class PushRecord:

    def __init__(self, repo: str, branch: str, games: list[str]) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOG_DIR / f"pushed_{datetime.now():%Y%m%d_%H%M%S}.txt"
        self._write(
            f"# Push record - started {_now()}\n# repo  : {mask_secret(repo)}\n"
            f"# branch: {branch}\n# games : {', '.join(games)}\n\n"
        )

    def _write(self, text: str) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass

    def note(self, text: str) -> None:
        self._write(f"[{_now()}] {text}\n")

    def batch_ok(self, idx: int, total: int, files: list[Path], base: Path) -> None:
        def size_of(path: Path) -> int:
            try:
                return path.stat().st_size
            except OSError:
                return 0
        total_size = sum(size_of(f) for f in files)
        lines = [f"[{_now()}] BATCH {idx}/{total} PUSHED OK - {len(files)} files, {fmt(total_size)}"]
        lines += [f"    {f.relative_to(base).as_posix()}  ({fmt(size_of(f))})" for f in files]
        self._write("\n".join(lines) + "\n")
        log("PUSHED", f"batch {idx}/{total}: {len(files)} files, {fmt(total_size)} (listed in {self.path.name})")

    def batch_failed(self, idx: int, total: int, error: str) -> None:
        first = (error.strip().splitlines() or ["unknown error"])[0]
        self.note(f"BATCH {idx}/{total} FAILED - {first}")


_VT_OK: bool | None = None


def _vt_enabled() -> bool:
    global _VT_OK
    if _VT_OK is not None:
        return _VT_OK
    ok = sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
    if ok and os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                ok = bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
            else:
                ok = False
        except Exception:
            ok = False
    _VT_OK = ok
    return ok


class ProgressBar:

    _bar_on = False

    def __init__(self, label: str, total: int, unit: str = "files", width: int = 24):
        self.label = label
        self.total = max(int(total), 1)
        self.unit = unit
        self.width = width
        self.current = 0
        self.last_draw = 0.0
        self.started = time.monotonic()
        self.is_tty = sys.stdout.isatty()
        self.vt = _vt_enabled()
        self.compact = DEBUG and unit in ("files", "bytes") and not self.vt
        self._last_extra = ""
        log("PROGRESS", f"{label}: start (total={self.total} {unit})")
        self._draw(force=True)

    @staticmethod
    def release() -> None:
        if ProgressBar._bar_on:
            rows = shutil.get_terminal_size((120, 30)).lines
            sys.stdout.write(f"\x1b7\x1b[r\x1b[{rows};1H\x1b[2K\x1b8")
            sys.stdout.flush()
            ProgressBar._bar_on = False

    def update(self, step: int = 1, extra: str = "") -> None:
        self.current = min(self.current + step, self.total)
        self._draw(extra=extra)

    def set(self, value: int, extra: str = "") -> None:
        self.current = min(max(int(value), 0), self.total)
        self._draw(extra=extra)

    def counter(self) -> str:
        return f"{min(self.current + 1, self.total)}/{self.total}"

    def _timing(self) -> str:
        elapsed = time.monotonic() - self.started
        text = fmt_time(elapsed)
        if 0 < self.current < self.total:
            text += f" ETA {fmt_time(elapsed * (self.total - self.current) / self.current)}"
        if self.unit == "bytes" and elapsed > 0.5 and self.current:
            text += f" {fmt(self.current / elapsed)}/s"
        return text

    def _stat(self) -> str:
        if self.unit == "%":
            return ""
        if self.unit == "bytes":
            return f"{fmt(self.current)}/{fmt(self.total)}"
        return f"{self.current}/{self.total} {self.unit}"

    def _draw_vt(self, force: bool, extra: str) -> None:
        now = time.monotonic()
        if not force and (now - self.last_draw) < 0.08 and self.current < self.total:
            return
        self.last_draw = now
        cols, rows = shutil.get_terminal_size((120, 30))
        ratio = self.current / self.total
        parts = [f" {self.label}", f"{ratio * 100:5.1f}%", self._stat(), self._timing(), extra]
        text = "  ".join(x for x in parts if x)[:cols].ljust(cols)
        filled = int(cols * ratio)
        painted = f"\x1b[30;107m{text[:filled]}\x1b[97;100m{text[filled:]}\x1b[0m"
        if not ProgressBar._bar_on:
            sys.stdout.write("\n\x1b[A")
            ProgressBar._bar_on = True
        sys.stdout.write(f"\x1b7\x1b[1;{rows - 1}r\x1b[{rows};1H\x1b[?7l{painted}\x1b[?7h\x1b8")
        sys.stdout.flush()

    def _draw(self, force: bool = False, extra: str = "") -> None:
        if extra:
            self._last_extra = extra
        else:
            extra = self._last_extra
        if self.vt:
            self._draw_vt(force, extra)
            return
        if self.compact:
            amount = fmt(self.total) if self.unit == "bytes" else f"{self.total} {self.unit}"
            if force and self.current == 0:
                print(f"  {self.label}: {amount} to process")
            elif force and self.current >= self.total:
                print(f"  {self.label}: done, {amount} in {fmt_time(time.monotonic() - self.started)}"
                      + (f" | {extra}" if extra else ""))
            return
        now = time.monotonic()
        if not force and (now - self.last_draw) < 0.08 and self.current < self.total:
            return
        self.last_draw = now
        ratio = self.current / self.total
        filled = int(self.width * ratio)
        bar = "#" * filled + "-" * (self.width - filled)
        stat = self._stat()
        head = f"  {self.label} [{bar}] {ratio * 100:5.1f}% ({stat}) {self._timing()}" if stat else \
               f"  {self.label} [{bar}] {ratio * 100:5.1f}% {self._timing()}"
        if not self.is_tty:
            if force or self.current >= self.total:
                print(head + (f" | {extra}" if extra else ""))
            return
        cols = shutil.get_terminal_size((120, 20)).columns - 1
        room = cols - len(head) - 3
        tail = ""
        if extra and room > 8:
            tail = " | " + (extra if len(extra) <= room else "..." + extra[-(room - 3):])
        sys.stdout.write("\r" + (head + tail)[:cols].ljust(cols))
        sys.stdout.flush()

    def finish(self, extra: str = "") -> None:
        self.current = self.total
        self._last_extra = extra
        if self.vt:
            ProgressBar.release()
            done = f"{fmt(self.total) if self.unit == 'bytes' else str(self.total) + ' ' + self.unit}, " if self.unit != "%" else ""
            note = f" | {extra}" if extra and extra != "Done" else ""
            print(f"  {self.label}: {done}{fmt_time(time.monotonic() - self.started)}{note}")
        else:
            self._draw(force=True, extra=extra)
            if self.is_tty and not self.compact:
                sys.stdout.write("\n")
                sys.stdout.flush()
        log("PROGRESS", f"{self.label}: done in {fmt_time(time.monotonic() - self.started)} {extra}".strip())


def fmt(number: int | float) -> str:
    value = float(number)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def safe_game_slug(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", name.strip())
    value = re.sub(r"\s+", " ", value).strip(" .")
    if value in ("", ".", ".."):
        value = "game"
    if len(value.encode("utf-8")) > 120:
        value = value.encode("utf-8")[:120].decode("utf-8", "ignore").rstrip(" .")
    return value or "game"


def _iter_source_files(root: Path):
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part.casefold() in {".git", "__pycache__", ".svn"} for part in rel_parts):
            continue
        yield path


def _pfs_sort_key(path: Path):
    match = PFS_RE.match(path.name)
    if not match:
        return (1, 0, path.name.casefold())
    suffix = match.group("volume")
    return (0, -1 if suffix is None else int(suffix), path.name.casefold())


def discover_games(folder: Path = GAMES) -> tuple[list[GameItem], list[str]]:
    items: list[GameItem] = []
    warnings: list[str] = []
    if not folder.is_dir():
        return items, warnings

    used: set[str] = set()
    for directory in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        if not directory.is_dir() or directory.name.startswith(".") or directory.is_symlink():
            continue
        files = sorted(_iter_source_files(directory), key=lambda p: p.relative_to(directory).as_posix().casefold())
        if not files:
            warnings.append(f"{directory.name}: empty folder, skipped")
            continue
        items.append(GameItem(directory.name, directory, files, loose=False))
        used.add(directory.name.casefold())

    groups: dict[tuple[str, str], list[Path]] = {}
    display_names: dict[tuple[str, str], str] = {}
    for path in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        if not path.is_file() or path.is_symlink():
            continue
        match = PFS_RE.match(path.name)
        if not match:
            continue
        key = (match.group("stem").casefold(), match.group("ext").casefold())
        groups.setdefault(key, []).append(path)
        if key not in display_names or match.group("volume") is None:
            display_names[key] = match.group("stem")

    for key, paths in groups.items():
        name = display_names[key]
        if name.casefold() in used:
            warnings.append(f"{name}: a folder with this name already exists; archive skipped")
            continue
        paths.sort(key=_pfs_sort_key)
        if not any(PFS_RE.match(p.name).group("volume") is None for p in paths):
            warnings.append(f"{name}: main .{key[1]} archive is missing")
        items.append(GameItem(name, folder, paths, loose=True))
        used.add(name.casefold())

    items.sort(key=lambda game: game.name.casefold())
    return items, warnings


def _is_pfs_archive(path: Path) -> bool:
    match = PFS_RE.match(path.name)
    if not match:
        return False
    if match.group("volume") is None:
        return True
    try:
        with path.open("rb") as handle:
            magic = handle.read(3).lower()
        return magic in (b"pf6", b"pf8")
    except OSError:
        return False


def load_local_env() -> None:
    if not ENV_FILE.is_file():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "GITHUB_TOKEN" and value.strip():
            os.environ["GITHUB_TOKEN"] = value.strip().strip('"').strip("'")


def push_url(repo: str) -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token and repo.startswith("https://"):
        return repo.replace("https://", f"https://x-access-token:{token}@", 1)
    return repo


def mask_secret(value: str) -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        value = value.replace(token, "***")
    return re.sub(r"(https://)[^@/\s]+@", r"\1***@", value)


def _cmd_text(args) -> str:
    return " ".join(f'"{a}"' if " " in str(a) else str(a) for a in args)


def run(args: list[str], cwd: Path = PROJECT, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess:
    log("CMD", f"{_cmd_text(args)}  (cwd={cwd})")
    if input_text:
        log("CMD", f"  stdin: {len(input_text.splitlines())} line(s)")
    started = time.monotonic()
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            input=input_text,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Program not found: {args[0]} (is it installed and on PATH?)") from exc
    log("RESULT", f"rc={result.returncode} in {time.monotonic() - started:.2f}s")
    if result.returncode:
        log("STDERR", (result.stderr or "").strip()[-2000:] or "(empty)")
        log("STDOUT", (result.stdout or "").strip()[-1000:] or "(empty)")
    elif DEBUG and (result.stdout or "").strip():
        log("STDOUT", result.stdout.strip()[-500:])
    if check and result.returncode:
        message = (result.stderr or result.stdout or "command failed").strip()
        raise RuntimeError(f"Command failed ({Path(args[0]).name}):\n{mask_secret(message)}")
    return result


PUSH_STALL_SECONDS = 240


def run_git_push_with_progress(cwd: Path, auth_url: str, branch: str, label: str = "Pushing") -> tuple[int, str]:
    cmd = ["git", "push", "--progress", auth_url, f"HEAD:refs/heads/{branch}"]
    log("CMD", f"{_cmd_text(cmd)}  (cwd={cwd})")
    push_started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    bar = ProgressBar(label, 100, unit="%")
    collected_err: list[str] = []
    lines: "queue.Queue[str | None]" = queue.Queue()

    def reader() -> None:
        buf = ""
        assert proc.stderr is not None
        try:
            while True:
                ch = proc.stderr.read(1)
                if ch == "":
                    break
                if ch in ("\r", "\n"):
                    if buf.strip():
                        lines.put(buf.strip())
                    buf = ""
                else:
                    buf += ch
            if buf.strip():
                lines.put(buf.strip())
        finally:
            lines.put(None)

    threading.Thread(target=reader, daemon=True).start()

    current_pct = 0
    phase = "Starting"
    detail = ""
    last_activity = time.monotonic()
    stalled = False
    done = False
    while not done:
        try:
            line = lines.get(timeout=1.0)
        except queue.Empty:
            line = ""
            if time.monotonic() - last_activity > PUSH_STALL_SECONDS and proc.poll() is None:
                stalled = True
                log("WARN", f"git push silent for {PUSH_STALL_SECONDS}s, killing it to retry")
                proc.kill()
            bar.set(current_pct, f"{phase} {detail} | {fmt_time(time.monotonic() - push_started)}".strip())
            continue
        if line is None:
            done = True
            continue
        last_activity = time.monotonic()
        collected_err.append(line)
        m_count = re.search(r"Counting objects:\s+(\d+)%", line)
        m_comp = re.search(r"Compressing objects:\s+(\d+)%", line)
        m_write = re.search(r"Writing objects:\s+(\d+)%(?:\s+\((\d+/\d+)\)(?:,\s+([^|]+))?(?:\|\s*(.+))?)?", line)
        m_res = re.search(r"Resolving deltas:\s+(\d+)%", line)
        if not (m_count or m_comp or m_write or m_res):
            log("GIT", line)
        if m_count:
            current_pct = max(current_pct, int(int(m_count.group(1)) * 0.10))
            phase, detail = "Counting", ""
        elif m_comp:
            current_pct = max(current_pct, 10 + int(int(m_comp.group(1)) * 0.25))
            phase, detail = "Compressing", ""
        elif m_write:
            current_pct = max(current_pct, 35 + int(int(m_write.group(1)) * 0.60))
            size = (m_write.group(3) or "").strip()
            speed = (m_write.group(4) or "").strip()
            phase, detail = "Uploading", " ".join(x for x in (size, speed) if x)
        elif m_res:
            current_pct = max(current_pct, 95 + int(int(m_res.group(1)) * 0.05))
            phase, detail = "Resolving", ""
        bar.set(current_pct, f"{phase} {detail}".strip())

    rc = proc.wait()
    log("RESULT", f"git push rc={rc} in {time.monotonic() - push_started:.1f}s")
    if rc == 0:
        bar.finish("Done")
    else:
        if bar.vt:
            ProgressBar.release()
        elif sys.stdout.isatty():
            sys.stdout.write("\n")
            sys.stdout.flush()
        if stalled:
            collected_err.append(f"push stalled: no output for {PUSH_STALL_SECONDS}s")
    return rc, "\n".join(collected_err)


def _scan_files(path: Path) -> dict[str, int]:
    found: dict[str, int] = {}
    for base, _dirs, names in os.walk(path):
        for name in names:
            full = os.path.join(base, name)
            try:
                found[os.path.relpath(full, path).replace("\\", "/")] = os.path.getsize(full)
            except OSError:
                pass
    return found


def run_with_activity(
    args: list[str],
    label: str,
    watch_dir: Path | None = None,
    cwd: Path = PROJECT,
    file_root: Path | None = None,
    file_prefix: str = "",
) -> subprocess.CompletedProcess:
    log("CMD", f"{_cmd_text(args)}  (cwd={cwd})")
    baseline = _scan_files(watch_dir) if watch_dir else {}
    sub = ""
    if watch_dir and file_root is not None:
        try:
            sub = watch_dir.resolve().relative_to(file_root.resolve()).as_posix()
        except ValueError:
            sub = ""
        if sub == ".":
            sub = ""
    started = time.monotonic()
    is_tty = sys.stdout.isatty()
    spinner = "|/-\\"
    tick = 0
    grown_files = grown_size = 0
    last_scan_at = 0.0
    scan_every = 0.5 if DEBUG else 1.0
    candidates: dict[str, int] = {}
    reported: set[str] = set()

    def scan(final: bool = False) -> None:
        nonlocal grown_files, grown_size
        current = _scan_files(watch_dir)
        new = {k: v for k, v in current.items() if baseline.get(k) != v}
        grown_files, grown_size = len(new), sum(new.values())
        if not (DEBUG and file_root is not None):
            return
        for rel, size in sorted(new.items()):
            if rel in reported:
                continue
            if final or candidates.get(rel) == size:
                reported.add(rel)
                candidates.pop(rel, None)
                shown = "/".join(part for part in (file_prefix, sub, rel) if part)
                log_file("extract", shown, size, f"#{len(reported)}")
            else:
                candidates[rel] = size

    if not is_tty:
        print(f"  {label}: running...")
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as out_f, \
            tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as err_f:
        try:
            proc = subprocess.Popen(args, cwd=cwd, stdout=out_f, stderr=err_f)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Program not found: {args[0]}") from exc
        while proc.poll() is None:
            now = time.monotonic()
            if watch_dir and now - last_scan_at >= scan_every:
                scan()
                last_scan_at = now
            if is_tty:
                extra = f" | written so far: {grown_files} files, {fmt(grown_size)}" if watch_dir else ""
                cols = shutil.get_terminal_size((120, 20)).columns - 1
                line = f"  {label} {spinner[tick % 4]} {fmt_time(now - started)}{extra}"
                sys.stdout.write("\r" + line[:cols].ljust(cols))
                sys.stdout.flush()
            tick += 1
            time.sleep(0.15)
        out_f.seek(0)
        err_f.seek(0)
        stdout, stderr = out_f.read(), err_f.read()
    took = time.monotonic() - started
    if watch_dir:
        scan(final=True)
    status = "done" if proc.returncode == 0 else f"FAILED (exit code {proc.returncode})"
    summary = f"  {label} {status} in {fmt_time(took)}" + (f" | {grown_files} files, {fmt(grown_size)}" if watch_dir else "")
    if is_tty:
        cols = shutil.get_terminal_size((120, 20)).columns - 1
        sys.stdout.write("\r" + summary[:cols].ljust(cols) + "\n")
        sys.stdout.flush()
    else:
        print(summary)
    log("RESULT", f"rc={proc.returncode} in {took:.2f}s | {grown_files} files, {fmt(grown_size)}")
    if proc.returncode:
        log("STDERR", stderr.strip()[-2000:] or "(empty)")
        log("STDOUT", stdout.strip()[-1000:] or "(empty)")
    elif DEBUG and stdout.strip():
        log("STDOUT", stdout.strip()[-500:])
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)


def find_pfs_tool() -> Path | str:
    exe_name = "pfs-rs.exe" if os.name == "nt" else "pfs-rs"
    local = TOOLS / exe_name
    if local.is_file():
        return local
    on_path = shutil.which("pfs-rs")
    if on_path:
        return on_path
    return download_pfs_tool()


def download_pfs_tool() -> Path:
    exe_name = "pfs-rs.exe" if os.name == "nt" else "pfs-rs"
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        os_key, ext = "windows", ".zip"
        arch_keys = ("x86_64", "amd64") if machine in ("amd64", "x86_64") else (machine,)
    elif system == "linux":
        os_key, ext = "linux", ".tar.gz"
        arch_keys = ("x86_64", "amd64") if machine in ("amd64", "x86_64") else (machine,)
    else:
        raise RuntimeError("Automatic pfs-rs download is not supported on this OS. Put pfs-rs in the tools folder.")

    headers = {"User-Agent": "ArtWebs-Games-Deployer/2.0", "Accept": "application/vnd.github+json"}
    request = urllib.request.Request(PFS_RELEASE_API, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(
            "Could not get the official pfs-rs release. "
            "Check your internet connection or put pfs-rs.exe in tools/. "
            f"Details: {exc}"
        ) from exc

    assets = release.get("assets") or []
    matches = []
    for asset in assets:
        name = str(asset.get("name", ""))
        lower = name.casefold()
        if ext not in lower or "pfs-rs" not in lower or not any(k in lower for k in arch_keys):
            continue
        if os_key not in lower:
            continue
        matches.append(asset)
    if not matches:
        raise RuntimeError(
            "No matching pfs-rs release was found for "
            f"{platform.system()} {platform.machine()}. Put pfs-rs in the tools folder."
        )

    asset = matches[0]
    download_url = asset.get("browser_download_url")
    if not download_url:
        raise RuntimeError("The GitHub release has no download link for pfs-rs.")
    TOOLS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pfs-rs-") as temp_dir:
        archive_path = Path(temp_dir) / str(asset.get("name", "pfs-rs-release"))
        print(f"Downloading PFS tool: {asset.get('name', 'pfs-rs release')}...")
        req = urllib.request.Request(download_url, headers={"User-Agent": "ArtWebs-Games-Deployer/2.0"})
        try:
            with urllib.request.urlopen(req, timeout=120) as response, archive_path.open("wb") as output:
                total_len = int(response.headers.get("Content-Length") or 0)
                bar = ProgressBar("Download pfs-rs", total_len or 1, unit="bytes")
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    output.write(chunk)
                    if total_len:
                        bar.update(len(chunk))
                bar.finish()
        except Exception as exc:
            raise RuntimeError(f"Could not download pfs-rs: {exc}") from exc

        extracted = None
        if archive_path.suffix.casefold() == ".zip":
            with zipfile.ZipFile(archive_path) as archive:
                for info in archive.infolist():
                    if Path(info.filename).name.casefold() in ("pfs-rs.exe", "pfs-rs"):
                        extracted = TOOLS / exe_name
                        with archive.open(info) as src, extracted.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                        break
        elif archive_path.name.casefold().endswith(".tar.gz"):
            with tarfile.open(archive_path, "r:gz") as archive:
                member = next((m for m in archive.getmembers() if Path(m.name).name == "pfs-rs" and m.isfile()), None)
                if member:
                    src = archive.extractfile(member)
                    if src:
                        extracted = TOOLS / exe_name
                        with src, extracted.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
        if not extracted or not extracted.is_file():
            raise RuntimeError("pfs-rs was not found in the downloaded release archive.")
        if os.name != "nt":
            extracted.chmod(0o755)
    print(f"PFS tool ready: {TOOLS / exe_name}")
    return TOOLS / exe_name


def _validate_extracted_tree(root: Path) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"The archive contains an unsupported symlink: {path.relative_to(root)}")
        if path.is_file():
            resolved = path.resolve()
            if root.resolve() not in resolved.parents:
                raise RuntimeError(f"A file path points outside the game folder: {path}")
            files.append(path)
    return files


def split_large_files_to_bin(root: Path, max_file_mb: int) -> Path | None:
    chunk_size = max_file_mb * MB
    large_files = sorted(
        [
            p for p in root.rglob("*")
            if p.is_file() and p.name != "manifest.json" and p.stat().st_size > chunk_size
        ],
        key=lambda p: p.relative_to(root).as_posix().casefold(),
    )
    if not large_files:
        return None

    print(f"  Splitting {len(large_files)} file(s) > {max_file_mb} MB into .bin parts...")
    manifest_entries = []
    for source in large_files:
        rel_path = source.relative_to(root).as_posix()
        original_size = source.stat().st_size
        file_hash = hashlib.sha256()
        parts_info = []
        part_index = 0
        bar = ProgressBar(f"Split {source.name}", original_size, unit="bytes")

        with source.open("rb") as src:
            while True:
                data = src.read(chunk_size)
                if not data:
                    break
                file_hash.update(data)
                part_name = f"{source.name}.part{part_index:03d}.bin"
                part_path = source.parent / part_name
                part_path.write_bytes(data)
                parts_info.append({
                    "path": part_path.relative_to(root).as_posix(),
                    "size": len(data),
                    "index": part_index,
                })
                part_index += 1
                log_file("split", f"{root.name}/{part_path.relative_to(root).as_posix()}", len(data), f"part {part_index}")
                bar.update(len(data), part_name)
        bar.finish(f"{len(parts_info)} parts")

        source.unlink()

        manifest_entries.append({
            "original_path": rel_path,
            "original_size": original_size,
            "sha256": file_hash.hexdigest(),
            "chunk_size": chunk_size,
            "parts": parts_info,
        })

    manifest_path = root / "manifest.json"
    manifest_data = {
        "version": 1,
        "max_chunk_mb": max_file_mb,
        "split_files": manifest_entries,
    }
    manifest_path.write_text(json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Created manifest.json ({len(manifest_entries)} split file entry)")
    return manifest_path


def _is_build_ready(output: Path, max_file_mb: int) -> bool:
    if not output.is_dir():
        return False
    limit = max_file_mb * MB
    has_files = False
    for p in output.rglob("*"):
        if p.is_file():
            has_files = True
            if p.stat().st_size > limit:
                return False
    return has_files


def prepare_game(item: GameItem, max_file_mb: int = DEFAULT_MAX_FILE_MB, reuse_existing: bool = True) -> PreparedGame:
    slug = safe_game_slug(item.name)
    output = BUILD / slug
    log("PREPARE", f"{item.name}: {len(item.files)} source files, slug={slug}, reuse_existing={reuse_existing}")

    preparing_flag = BUILD / f"{slug}.preparing"
    if preparing_flag.exists():
        print("  Previous preparation did not finish (crash or closed window). Preparing again from scratch.")
        log("PREPARE", f"{item.name}: found {preparing_flag.name}, discarding partial build")
    if reuse_existing and not preparing_flag.exists() and _is_build_ready(output, max_file_mb):
        final_files = _validate_extracted_tree(output)
        total_bytes = sum(path.stat().st_size for path in final_files)
        print(f"  Ready: {len(final_files)} files, {fmt(total_bytes)}")
        return PreparedGame(item=item, slug=slug, output=output, file_count=len(final_files), total_bytes=total_bytes)

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    preparing_flag.write_text(f"started {_now()}\n", encoding="utf-8")

    loose_sources = [s for s in item.files if not PFS_RE.match(s.name)]
    if loose_sources:
        SESSION.update(detail=f"copying {len(loose_sources)} file(s)")
        copy_bar = ProgressBar("Copying files", len(loose_sources), unit="files")
        for source in loose_sources:
            relative = source.relative_to(item.root)
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            log_file("copy", f"{slug}/{relative.as_posix()}", source.stat().st_size, copy_bar.counter())
            copy_bar.update(1, relative.as_posix())
        copy_bar.finish()

    archives = [path for path in item.files if _is_pfs_archive(path)]
    if archives:
        tool = find_pfs_tool()
        log("PFS", f"tool: {tool}")
        ordered = sorted(archives, key=_pfs_sort_key)
        for number, archive in enumerate(ordered, 1):
            rel_parent = archive.parent.relative_to(item.root)
            destination = output / rel_parent
            destination.mkdir(parents=True, exist_ok=True)
            print(f"  [{number}/{len(ordered)}] Extracting {archive.name} ({fmt(archive.stat().st_size)})")
            SESSION.update(detail=f"extracting {archive.name} ({number}/{len(ordered)})")
            log("PFS", f"extracting {archive.name} -> {destination}")
            result = run_with_activity(
                [str(tool), "--force", "extract", str(archive.resolve()), str(destination.resolve())],
                label="pfs-rs",
                watch_dir=destination,
                file_root=output,
                file_prefix=slug,
            )
            log("PFS", f"finished {archive.name}: exit code {result.returncode}")
            if result.returncode:
                message = (result.stderr or result.stdout or "PFS extraction failed").strip()
                raise RuntimeError(f"Could not extract {archive.name}:\n{mask_secret(message)}")
    elif any(PFS_RE.match(path.name) for path in item.files):
        raise RuntimeError(
            f"{item.name}: PFS files were found, but no readable main archive was found. "
            "Keep the main .pfs file and all related volume files together."
        )

    _validate_extracted_tree(output)
    SESSION.update(detail="splitting large files")
    split_large_files_to_bin(output, max_file_mb)

    final_files = _validate_extracted_tree(output)
    if not final_files:
        raise RuntimeError(f"{item.name}: no files were found after extraction.")
    total_bytes = sum(path.stat().st_size for path in final_files)
    preparing_flag.unlink(missing_ok=True)
    print(f"  Ready: {len(final_files)} files, {fmt(total_bytes)}")
    return PreparedGame(item=item, slug=slug, output=output, file_count=len(final_files), total_bytes=total_bytes)


def repo_parts(repo: str) -> tuple[str, str]:
    clean = repo.strip().removesuffix(".git")
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+)$", clean, re.IGNORECASE)
    if not match:
        raise RuntimeError("Use a GitHub repository URL, for example: https://github.com/OWNER/REPO.git")
    return match.group(1), match.group(2)


LOCK_RE = re.compile(r"Unable to create '([^']+\.lock)'", re.IGNORECASE)


def _git_running() -> bool:
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq git.exe", "/NH"],
                                 capture_output=True, text=True, errors="replace").stdout
            return "git.exe" in out.lower()
        return subprocess.run(["pgrep", "-x", "git"], capture_output=True).returncode == 0
    except Exception:
        return False


def clear_stale_git_locks(repo_dir: Path) -> int:
    git_dir = repo_dir / ".git"
    if not git_dir.is_dir():
        return 0
    locks = [p for p in git_dir.glob("*.lock") if p.is_file()]
    refs = git_dir / "refs"
    if refs.is_dir():
        locks += [p for p in refs.rglob("*.lock") if p.is_file()]
    removed = 0
    for lock in locks:
        if _git_running():
            log("LOCK", f"git is running, waiting for {lock.name} to be released")
            for _ in range(30):
                if not lock.exists():
                    break
                time.sleep(0.5)
            if not lock.exists():
                continue
            if time.time() - lock.stat().st_mtime < 120:
                raise RuntimeError(
                    f"{lock} is held by a running git process.\n"
                    "Close other programs using this repo (VS Code, GitHub Desktop, another terminal) and try again."
                )
        try:
            lock.unlink()
            removed += 1
            log("LOCK", f"removed stale lock: {lock}")
            print(f"  Removed stale git lock: {lock.name}")
        except OSError as exc:
            raise RuntimeError(f"Could not remove git lock {lock}: {exc}") from exc
    return removed


def git_at(cwd: Path, *args: str, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["git", *args]
    result = run(cmd, cwd=cwd, check=False, input_text=input_text)
    if result.returncode and LOCK_RE.search(result.stderr or ""):
        log("WARN", "git lock file detected, clearing stale lock and retrying")
        if clear_stale_git_locks(cwd):
            result = run(cmd, cwd=cwd, check=False, input_text=input_text)
    if check and result.returncode:
        message = (result.stderr or result.stdout or "command failed").strip()
        raise RuntimeError(f"Command failed (git):\n{mask_secret(message)}")
    return result


def configure_git_transport(cwd: Path) -> None:
    settings = [
        ("http.postBuffer", "524288000"),
        ("http.version", "HTTP/1.1"),
        ("http.lowSpeedLimit", "1000"),
        ("http.lowSpeedTime", "600"),
        ("pack.window", "0"),
        ("pack.windowMemory", "256m"),
        ("pack.packSizeLimit", "512m"),
        ("pack.threads", "2"),
        ("core.compression", "1"),
        ("core.autocrlf", "false"),
        ("core.longpaths", "true"),
    ]
    for key, val in settings:
        git_at(cwd, "config", key, val, check=False)


def ensure_checkout(repo: str, branch: str) -> Path:
    auth_url = push_url(repo)
    REMOTE_WORKTREE.mkdir(parents=True, exist_ok=True)
    if not (REMOTE_WORKTREE / ".git").is_dir():
        run(["git", "init", "-b", branch], cwd=REMOTE_WORKTREE)
        git_at(REMOTE_WORKTREE, "remote", "add", "origin", repo)

    removed = clear_stale_git_locks(REMOTE_WORKTREE)
    if removed:
        log("LOCK", f"cleared {removed} stale lock(s) before syncing")
    configure_git_transport(REMOTE_WORKTREE)

    origin = git_at(REMOTE_WORKTREE, "remote", "get-url", "origin", check=False)
    if origin.returncode or origin.stdout.strip().removesuffix(".git") != repo.strip().removesuffix(".git"):
        git_at(REMOTE_WORKTREE, "remote", "set-url", "origin", repo, check=False)

    if not git_at(REMOTE_WORKTREE, "config", "user.name", check=False).stdout.strip():
        git_at(REMOTE_WORKTREE, "config", "user.name", "ArtWebs Games Deployer")
        git_at(REMOTE_WORKTREE, "config", "user.email", "artwebs-deployer@example.invalid")

    log("SYNC", "syncing GitHub branch")
    remote_check = run(["git", "ls-remote", "--heads", auth_url, f"refs/heads/{branch}"], check=False)
    if remote_check.returncode:
        raise RuntimeError(f"Could not access the GitHub repository. Check the URL and token.\n{mask_secret(remote_check.stderr or remote_check.stdout)}")
    if (remote_check.stdout or "").strip():
        git_at(REMOTE_WORKTREE, "fetch", "--depth=1", auth_url, branch)
        git_at(REMOTE_WORKTREE, "reset", "--mixed", "FETCH_HEAD")
    else:
        current = git_at(REMOTE_WORKTREE, "branch", "--show-current", check=False).stdout.strip()
        if current != branch:
            git_at(REMOTE_WORKTREE, "checkout", "-B", branch)
    return REMOTE_WORKTREE


def git_stage_paths_safe(cwd: Path, rel_paths: list[str], extra_flags: list[str] | None = None, show_progress: bool = False) -> None:
    if not rel_paths:
        return
    flags = extra_flags or ["-f"]
    bar = ProgressBar("Git add", len(rel_paths), unit="files") if show_progress else None
    step = 200
    for start in range(0, len(rel_paths), step):
        chunk = rel_paths[start : start + step]
        if DEBUG:
            for offset, rel_path in enumerate(chunk, start + 1):
                log_file("stage", rel_path, counter=f"{offset}/{len(rel_paths)}")
        payload = "\n".join(chunk) + "\n"
        res = git_at(cwd, "add", *flags, "--pathspec-from-file=-", check=False, input_text=payload)
        if res.returncode:
            log("WARN", f"git add --pathspec-from-file failed, using fallback: {(res.stderr or '').strip()[:300]}")
            for sub_start in range(0, len(chunk), 25):
                sub_chunk = chunk[sub_start : sub_start + 25]
                git_at(cwd, "add", *flags, "--", *sub_chunk)
        if bar:
            bar.update(len(chunk), chunk[-1])
    if bar:
        bar.finish()


class PushFailed(RuntimeError):
    pass


def push_checkout(repo: str, branch: str, relatives: list[str], message: str, label: str = "Pushing", attempts: int = 4) -> None:
    auth_url = push_url(repo)
    error = "push failed"
    for attempt in range(1, attempts + 1):
        rc, output_err = run_git_push_with_progress(REMOTE_WORKTREE, auth_url, branch, label=label)
        if rc == 0:
            return
        error = (output_err or "push failed").strip()
        if re.search(r"fetch first|non-fast-forward|\[rejected\]", error, re.IGNORECASE):
            print("  GitHub changed during upload. Syncing and retrying...")
            git_at(REMOTE_WORKTREE, "fetch", "--depth=1", auth_url, branch)
            git_at(REMOTE_WORKTREE, "reset", "--mixed", "FETCH_HEAD")
            git_stage_paths_safe(REMOTE_WORKTREE, relatives, extra_flags=["-f"], show_progress=False)
            staged = git_at(REMOTE_WORKTREE, "diff", "--cached", "--quiet", check=False)
            if staged.returncode == 0:
                return
            git_at(REMOTE_WORKTREE, "commit", "-m", message)
            continue
        if attempt < attempts:
            wait_sec = 4 * attempt
            print(f"  Push interrupted (attempt {attempt}/{attempts}). Retrying in {wait_sec}s...")
            time.sleep(wait_sec)
    raise PushFailed(f"GitHub upload failed:\n{mask_secret(error)}")


MIN_SPLIT_BYTES = 20 * MB


def _split_in_half(files: list[Path]) -> tuple[list[Path], list[Path]]:
    total = sum(f.stat().st_size for f in files)
    acc = 0
    for i, f in enumerate(files, 1):
        acc += f.stat().st_size
        if acc >= total / 2 or i == len(files) - 1:
            return files[:i], files[i:]
    return files, []


def push_files_adaptive(checkout: Path, repo: str, branch: str, files: list[Path], message: str, label: str) -> int:
    rel_paths = [f.relative_to(checkout).as_posix() for f in files]
    git_stage_paths_safe(checkout, rel_paths, extra_flags=["-f"], show_progress=True)
    if git_at(checkout, "diff", "--cached", "--quiet", check=False).returncode == 0:
        print("  Already up to date, skipped.")
        return 0
    size = sum(f.stat().st_size for f in files)
    git_at(checkout, "commit", "-m", message)
    can_split = len(files) > 1 and (size > MIN_SPLIT_BYTES or len(files) > 200)
    try:
        push_checkout(repo, branch, rel_paths, message, label=label, attempts=2 if can_split else 5)
        return 1
    except PushFailed:
        if not can_split:
            raise
    print(f"  Push of {len(files)} files ({fmt(size)}) keeps failing - splitting it in two smaller pushes...")
    log("SPLIT", f"{label}: {len(files)} files {fmt(size)} -> halves")
    git_at(checkout, "reset", "--mixed", "HEAD~1")
    first, second = _split_in_half(files)
    count = push_files_adaptive(checkout, repo, branch, first, message + " [part A]", label + "a")
    if second:
        count += push_files_adaptive(checkout, repo, branch, second, message + " [part B]", label + "b")
    return count


def _check_github_file_limits(root: Path, max_file_mb: int) -> None:
    limit = max_file_mb * MB
    large = [(p, p.stat().st_size) for p in root.rglob("*") if p.is_file() and p.stat().st_size > limit]
    if large:
        details = "\n".join(f"  {p.relative_to(root)} - {fmt(size)}" for p, size in large[:20])
        extra = "\n  ..." if len(large) > 20 else ""
        raise RuntimeError(
            f"{len(large)} files still exceed the {max_file_mb} MB limit.\n{details}{extra}"
        )


def _sync_tree_with_progress(src_root: Path, dst_root: Path, label: str = "Staging files") -> None:
    src_files = [p for p in src_root.rglob("*") if p.is_file()]
    dst_root.mkdir(parents=True, exist_ok=True)
    expected_rel = {p.relative_to(src_root) for p in src_files}

    for existing in list(dst_root.rglob("*")):
        if existing.is_file() and existing.relative_to(dst_root) not in expected_rel:
            existing.unlink()

    bar = ProgressBar(label, len(src_files), unit="files")
    copied = skipped = 0
    for src in src_files:
        rel = src.relative_to(src_root)
        dst = dst_root / rel
        if dst.is_file() and dst.stat().st_size == src.stat().st_size:
            skipped += 1
            log_file("skip", f"games/{dst_root.name}/{rel.as_posix()}", counter=bar.counter())
            bar.update(1, rel.as_posix())
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
        log_file("sync", f"games/{dst_root.name}/{rel.as_posix()}", src.stat().st_size, bar.counter())
        bar.update(1, rel.as_posix())
    bar.finish(f"{copied} copied, {skipped} unchanged")
    log("SYNC", f"{src_root} -> {dst_root}: copied={copied} unchanged={skipped}")


def _get_pending_files(checkout: Path, game_slugs: list[str]) -> list[Path]:
    pending_rel: set[str] = set()
    for slug in game_slugs:
        res = git_at(
            checkout,
            "-c", "core.quotepath=false",
            "status", "--porcelain", "-z", "-uall", "--", f"games/{slug}",
            check=False,
        )
        entries = (res.stdout or "").split("\0")
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            status_code = entry[:2]
            path_str = entry[3:]
            if "R" in status_code or "C" in status_code:
                i += 1
            if status_code.strip() != "D" and "D" not in status_code[:1]:
                pending_rel.add(path_str)

    pending_paths = [checkout / Path(rel) for rel in sorted(pending_rel, key=str.casefold)]
    return [p for p in pending_paths if p.is_file()]


def _split_files_into_batches(files: list[Path], batch_limit_bytes: int) -> list[list[Path]]:
    batches: list[list[Path]] = []
    current_batch: list[Path] = []
    current_size = 0
    for path in files:
        size = path.stat().st_size
        if current_batch and ((current_size + size) > batch_limit_bytes or len(current_batch) >= 1500):
            batches.append(current_batch)
            current_batch = []
            current_size = 0
        current_batch.append(path)
        current_size += size
    if current_batch:
        batches.append(current_batch)
    return batches


def upload_games_batch(
    prepared_list: list[PreparedGame],
    repo: str,
    branch: str,
    max_file_mb: int,
    batch_push_mb: int = DEFAULT_BATCH_PUSH_MB,
) -> None:
    if not prepared_list:
        return

    owner, repo_name = repo_parts(repo)
    record = PushRecord(repo, branch, [p.item.name for p in prepared_list])
    SESSION.update(phase="sync", detail="connecting to GitHub", push_record=str(record.path))
    log("PUSHRECORD", f"writing list of pushed files to {record.path}")
    checkout = ensure_checkout(repo, branch)
    batch_limit_bytes = max(batch_push_mb, 50) * MB

    total_prepared_files = 0
    for number, prep in enumerate(prepared_list, 1):
        print(f"\n> Sync {prep.item.name}")
        SESSION.update(phase="sync", current_game=prep.item.name, detail=f"copying files into repo ({number}/{len(prepared_list)})")
        target = checkout / "games" / prep.slug
        _sync_tree_with_progress(prep.output, target, label="Syncing to repo")
        total_prepared_files += prep.file_count

    slugs = [p.slug for p in prepared_list]
    pending_files = _get_pending_files(checkout, slugs)
    already_uploaded_count = max(total_prepared_files - len(pending_files), 0)

    if already_uploaded_count > 0 and pending_files:
        print(
            f"\n  {already_uploaded_count} files already on GitHub (skipped), {len(pending_files)} left"
        )

    total_bytes = sum(f.stat().st_size for f in pending_files)
    total_files = len(pending_files)
    log("PENDING", f"{total_files} file(s), {fmt(total_bytes)} not yet on GitHub (already uploaded: {already_uploaded_count})")
    game_names = ", ".join(p.item.name for p in prepared_list)
    record.note(f"Pending: {total_files} file(s), {fmt(total_bytes)} | already on GitHub: {already_uploaded_count}")
    SESSION.update(phase="upload", detail=f"{total_files} file(s) to push", total_pending_files=total_files)

    if total_files == 0:
        for prep in prepared_list:
            git_at(checkout, "add", "-A", "--", f"games/{prep.slug}")
        if git_at(checkout, "diff", "--cached", "--quiet", check=False).returncode:
            msg = f"Clean up removed files: {game_names}"
            git_at(checkout, "commit", "-m", msg)
            push_checkout(repo, branch, [f"games/{s}" for s in slugs], msg, label="Cleanup Push")
        record.note(f"Nothing to push: all {already_uploaded_count} file(s) are already on GitHub.")
        print("\n  Already up to date on GitHub")
        return

    print(f"\n  {total_files} files, {fmt(total_bytes)} to push")

    batches = _split_files_into_batches(pending_files, batch_limit_bytes)
    print(f"  {len(batches)} batch(es)")

    for idx, batch_files in enumerate(batches, 1):
        batch_bytes = sum(f.stat().st_size for f in batch_files)
        rel_paths = [f.relative_to(checkout).as_posix() for f in batch_files]
        print(f"\n> Batch {idx}/{len(batches)}: {len(batch_files)} files, {fmt(batch_bytes)}")
        log("BATCH", f"{idx}/{len(batches)}: first={rel_paths[0]} last={rel_paths[-1]}")
        msg = (
            f"Upload games ({idx}/{len(batches)}): {game_names}"
            if len(batches) > 1
            else f"Upload games: {game_names}"
        )
        SESSION.update(phase="upload", batch=idx, batches=len(batches),
                       detail=f"batch {idx}/{len(batches)}: pushing {len(batch_files)} files ({fmt(batch_bytes)})")
        try:
            pushes = push_files_adaptive(checkout, repo, branch, batch_files, msg, label=f"Push {idx}/{len(batches)}")
        except Exception as exc:
            record.batch_failed(idx, len(batches), str(exc))
            print(f"  Push record so far: {record.path}")
            raise
        if pushes == 0:
            record.note(f"BATCH {idx}/{len(batches)} skipped (already up to date)")
            SESSION.update(batches_done=idx)
            continue
        record.batch_ok(idx, len(batches), batch_files, checkout)
        SESSION.update(batches_done=idx, detail=f"batch {idx}/{len(batches)} pushed")

    for prep in prepared_list:
        git_at(checkout, "add", "-A", "--", f"games/{prep.slug}")
    if git_at(checkout, "diff", "--cached", "--quiet", check=False).returncode:
        msg = f"Clean up removed files: {game_names}"
        git_at(checkout, "commit", "-m", msg)
        push_checkout(repo, branch, [f"games/{s}" for s in slugs], msg, label="Cleanup Push")

    record.note("All batches finished.")
    print("\nAll selected games uploaded successfully!")
    print(f"Push record (every file pushed): {record.path}")
    for prep in prepared_list:
        url = (
            f"https://github.com/{owner}/{repo_name}/tree/"
            f"{urllib.parse.quote(branch, safe='')}/games/{urllib.parse.quote(prep.slug, safe='')}"
        )
        print(f"  - {prep.item.name}: {prep.file_count} files ({fmt(prep.total_bytes)})")
        print(f"    {url}")


def github_api(repo: str, method: str, endpoint: str, body: dict | None = None,
               allow_404: bool = False):
    owner, repo_name = repo_parts(repo)
    url = f"https://api.github.com/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo_name)}/{endpoint.lstrip('/')}"
    headers = {
        "User-Agent": "ArtWebs-Games-Deployer/2.0",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        if allow_404 and exc.code == 404:
            return None
        raise RuntimeError(f"GitHub API {method} {endpoint} failed ({exc.code}): {detail[:1200]}") from exc
    except Exception as exc:
        raise RuntimeError(f"Could not connect to the GitHub API: {exc}") from exc


def list_remote_games(repo: str, branch: str) -> list[str]:
    branch_q = urllib.parse.quote(branch, safe="")
    entries = github_api(repo, "GET", f"contents/games?ref={branch_q}", allow_404=True)
    if not entries:
        return []
    if not isinstance(entries, list):
        raise RuntimeError("GitHub returned an invalid games folder list.")
    return sorted(
        [str(item["name"]) for item in entries if item.get("type") == "dir" and item.get("name")],
        key=str.casefold,
    )


def game_tree_url(repo: str, branch: str, slug: str) -> str:
    owner, repo_name = repo_parts(repo)
    return (
        f"https://github.com/{owner}/{repo_name}/tree/"
        f"{urllib.parse.quote(branch, safe='')}/games/{urllib.parse.quote(slug, safe='')}"
    )


def choose_remote_game(games: list[str], prompt: str) -> str | None:
    if not games:
        print("No games found on GitHub.")
        return None
    picked = _pick_numbers(games, multi=False)
    return games[picked[0]] if picked else None


def view_games(cfg: dict) -> None:
    repo, branch = str(cfg["repo"]).strip(), str(cfg.get("branch") or "main").strip()
    games = list_remote_games(repo, branch)
    selected = choose_remote_game(games, "Open a game")
    if not selected:
        return
    url = game_tree_url(repo, branch, selected)
    print(f"\nOpening: {url}")
    webbrowser.open(url)
    print("This opens the game files on GitHub. It does not play the game in a browser.")


def _git_blob_sha(path: Path, bar: "ProgressBar | None" = None) -> str:
    size = path.stat().st_size
    digest = hashlib.sha1()
    digest.update(f"blob {size}\0".encode())
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(8 * MB)
            if not chunk:
                break
            digest.update(chunk)
            if bar:
                bar.update(len(chunk), path.name)
    return digest.hexdigest()


def _remote_blob_map(checkout: Path, slug: str) -> dict[str, str]:
    res = git_at(checkout, "ls-tree", "-r", "-z", "HEAD", "--", f"games/{slug}", check=False)
    remote: dict[str, str] = {}
    for entry in (res.stdout or "").split("\0"):
        if "\t" not in entry:
            continue
        meta, path = entry.split("\t", 1)
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            remote[path] = parts[2]
    return remote


def _sync_sources_into_build(item: GameItem, output: Path, max_file_mb: int) -> int:
    limit = max_file_mb * MB
    manifest_path = output / "manifest.json"
    manifest: dict = {"version": 1, "max_chunk_mb": max_file_mb, "split_files": []}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    split_map = {e["original_path"]: e for e in manifest.get("split_files", [])}
    manifest_changed = False
    updated = 0
    loose = [f for f in item.files if not PFS_RE.match(f.name)]
    for src in loose:
        rel = src.relative_to(item.root).as_posix()
        if rel == "manifest.json":
            continue
        size = src.stat().st_size
        if size > limit:
            entry = split_map.get(rel)
            if entry and entry.get("original_size") == size:
                continue
            tmp = BUILD / ".split_tmp"
            shutil.rmtree(tmp, ignore_errors=True)
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tmp / rel)
            tmp_manifest = split_large_files_to_bin(tmp, max_file_mb)
            if tmp_manifest and tmp_manifest.is_file():
                new_manifest = json.loads(tmp_manifest.read_text(encoding="utf-8"))
                if entry:
                    for part in entry.get("parts", []):
                        (output / part["path"]).unlink(missing_ok=True)
                for new_entry in new_manifest.get("split_files", []):
                    for part in new_entry["parts"]:
                        target = output / part["path"]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(tmp / part["path"]), str(target))
                    split_map[new_entry["original_path"]] = new_entry
                manifest_changed = True
                updated += 1
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            dst = output / rel
            if dst.is_file() and dst.stat().st_size == size and filecmp.cmp(src, dst, shallow=False):
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            updated += 1
    if manifest_changed:
        manifest["split_files"] = list(split_map.values())
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return updated


def _push_files_in_batches(checkout: Path, repo: str, branch: str, files: list[Path], slug: str, batch_push_mb: int) -> int:
    record = PushRecord(repo, branch, [slug])
    batches = _split_files_into_batches(files, max(batch_push_mb, 50) * MB)
    pushed_files = 0
    print(f"\nPushing in {len(batches)} batch(es)...")
    for idx, batch in enumerate(batches, 1):
        size = sum(f.stat().st_size for f in batch)
        print(f"\n> Batch {idx}/{len(batches)}: {len(batch)} files, {fmt(size)}")
        msg = f"Update {slug} ({idx}/{len(batches)})" if len(batches) > 1 else f"Update {slug}"
        try:
            pushes = push_files_adaptive(checkout, repo, branch, batch, msg, label=f"Push {idx}/{len(batches)}")
        except Exception as exc:
            record.batch_failed(idx, len(batches), str(exc))
            raise
        if pushes:
            pushed_files += len(batch)
            record.batch_ok(idx, len(batches), batch, checkout)
        else:
            record.note(f"BATCH {idx}/{len(batches)} skipped (identical to GitHub)")
    print(f"Push record: {record.path}")
    return pushed_files


def _print_diff_list(title: str, rels: list[str], sizes: dict[str, int], limit: int = 15) -> None:
    if not rels:
        return
    print(f"\n  {title} ({len(rels)}):")
    for rel in rels[:limit]:
        print(f"    {rel}  ({fmt(sizes.get(rel, 0))})")
    if len(rels) > limit:
        print(f"    ... and {len(rels) - limit} more")


def _norm_name(name: str) -> str:
    return re.sub(r"[^0-9a-z\u0080-\uffff]+", "", safe_game_slug(name).casefold())


def _find_local_game(slug: str) -> GameItem | None:
    items, _ = discover_games(GAMES)
    for item in items:
        if safe_game_slug(item.name).casefold() == slug.casefold():
            return item
    for item in items:
        if _norm_name(item.name) == _norm_name(slug):
            return item
    return None


def _compare_and_upload_local(repo: str, branch: str, slug: str, item: GameItem, max_file_mb: int, batch_push_mb: int) -> None:
    print(f"\nFound in games/: {item.name} ({fmt(item.size)}, {len(item.files)} source files)")
    print("\n> Prepare")
    prep = prepare_game(item, max_file_mb=max_file_mb, reuse_existing=True)
    refreshed = _sync_sources_into_build(item, prep.output, max_file_mb)
    if refreshed:
        print(f"  Picked up {refreshed} new/changed loose file(s) from games/{item.name}")
    _check_github_file_limits(prep.output, max_file_mb)

    print("\n> Compare")
    checkout = ensure_checkout(repo, branch)
    remote = _remote_blob_map(checkout, slug)
    prefix = f"games/{slug}/"
    local_files = sorted((f for f in prep.output.rglob("*") if f.is_file()),
                         key=lambda f: f.relative_to(prep.output).as_posix().casefold())
    bar = ProgressBar("Comparing", sum(f.stat().st_size for f in local_files) or 1, unit="bytes")
    new: list[Path] = []
    changed: list[Path] = []
    same = 0
    seen: set[str] = set()
    sizes: dict[str, int] = {}
    for f in local_files:
        rel = f.relative_to(prep.output).as_posix()
        full = prefix + rel
        seen.add(full)
        sizes[rel] = f.stat().st_size
        sha = _git_blob_sha(f, bar)
        if full not in remote:
            new.append(f)
        elif remote[full] != sha:
            changed.append(f)
        else:
            same += 1
    bar.finish()
    only_remote = sorted(r[len(prefix):] for r in remote if r not in seen)

    rel_of = lambda f: f.relative_to(prep.output).as_posix()
    print(f"\nLocal: {len(local_files)} files | GitHub: {len(remote)} files")
    print(f"  Identical : {same}")
    print(f"  Missing on GitHub (new)     : {len(new)}")
    print(f"  Different (will be replaced): {len(changed)}")
    print(f"  Only on GitHub (kept, never deleted): {len(only_remote)}")
    _print_diff_list("New files", [rel_of(f) for f in new], sizes)
    _print_diff_list("Different files", [rel_of(f) for f in changed], sizes)
    if only_remote:
        _print_diff_list("Only on GitHub", only_remote, {})

    todo = new + changed
    if not todo:
        print("\nGitHub is already complete and identical to your local game. Nothing to upload.")
        return
    total = sum(f.stat().st_size for f in todo)
    print(f"\nTo upload: {len(todo)} file(s), {fmt(total)}")
    if not ask_yes_no("Upload only these files?", default=True):
        return

    print("\n> Upload")
    target = checkout / "games" / slug
    work_files: list[Path] = []
    bar = ProgressBar("Staging", len(todo), unit="files")
    for f in todo:
        rel = f.relative_to(prep.output)
        dst = target / rel
        if not (dst.is_file() and dst.stat().st_size == f.stat().st_size and filecmp.cmp(f, dst, shallow=False)):
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
        work_files.append(dst)
        bar.update(1, rel.as_posix())
    bar.finish()
    pushed = _push_files_in_batches(checkout, repo, branch, work_files, slug, batch_push_mb)
    if pushed:
        print(f"\nDone. {pushed} file(s) uploaded: {game_tree_url(repo, branch, slug)}")
    else:
        print("\nNothing was pushed (GitHub already had these files).")


def _patch_from_path(repo: str, branch: str, slug: str, max_file_mb: int, batch_push_mb: int) -> None:
    print(f"\n{slug} was not found in the games folder.")
    print("Give the folder (or single file) that holds the patch / missing files.")
    print("Folder layout must match the game: patch/system/ui/x.lua  ->  games/" + slug + "/system/ui/x.lua")
    raw = input("\nPatch folder or file (drag it here, 0 to go back): ").strip().strip('"').strip("'").strip()
    if not raw or raw == "0":
        return
    source = Path(raw).expanduser()
    if not source.exists():
        print(f"Not found: {source}")
        return

    if source.is_dir():
        src_files = [f for f in source.rglob("*")
                     if f.is_file() and ".git" not in f.relative_to(source).parts
                     and f.name.lower() not in ("thumbs.db", "desktop.ini", ".ds_store")]
        rel_of = lambda f: f.relative_to(source)
    else:
        src_files = [source]
        rel_of = lambda f: Path(f.name)
    if not src_files:
        print("That folder has no files.")
        return
    total = sum(f.stat().st_size for f in src_files)
    print(f"\nPatch: {len(src_files)} file(s), {fmt(total)}")
    print(f"Target: games/{slug} on {branch} (nothing on GitHub will be deleted)")
    if not ask_yes_no("Add / replace these files on GitHub?", default=True):
        return

    build = PROJECT / ".patch_build" / safe_game_slug(slug)
    shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True, exist_ok=True)
    try:
        bar = ProgressBar("Copying patch", len(src_files), unit="files")
        for f in src_files:
            dst = build / rel_of(f)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            bar.update(1, rel_of(f).as_posix())
        bar.finish()

        patch_manifest = split_large_files_to_bin(build, max_file_mb)
        _check_github_file_limits(build, max_file_mb)

        checkout = ensure_checkout(repo, branch)
        target = checkout / "games" / slug
        remote = _remote_blob_map(checkout, slug)
        if patch_manifest and patch_manifest.is_file():
            old = git_at(checkout, "show", f"HEAD:games/{slug}/manifest.json", check=False)
            if old.returncode == 0 and old.stdout.strip():
                try:
                    merged = json.loads(old.stdout)
                    new_m = json.loads(patch_manifest.read_text(encoding="utf-8"))
                    keep = {e["original_path"]: e for e in merged.get("split_files", [])}
                    for e in new_m.get("split_files", []):
                        keep[e["original_path"]] = e
                    merged["split_files"] = list(keep.values())
                    patch_manifest.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
                    print("  Merged with the game's existing manifest.json")
                except (ValueError, KeyError) as exc:
                    log("WARN", f"could not merge manifest.json: {exc}")

        build_files = sorted((f for f in build.rglob("*") if f.is_file()), key=lambda f: f.relative_to(build).as_posix().casefold())
        todo: list[Path] = []
        new_count = 0
        for f in build_files:
            full = f"games/{slug}/{f.relative_to(build).as_posix()}"
            if full not in remote:
                new_count += 1
                todo.append(f)
            elif remote[full] != _git_blob_sha(f):
                todo.append(f)
        print(f"\n{new_count} new, {len(todo) - new_count} different, {len(build_files) - len(todo)} identical (skipped).")
        if not todo:
            print("Nothing to upload: GitHub already has exactly these files.")
            return

        work_files: list[Path] = []
        for f in todo:
            dst = target / f.relative_to(build)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dst)
            work_files.append(dst)
        pushed = _push_files_in_batches(checkout, repo, branch, work_files, slug, batch_push_mb)
        if pushed:
            print(f"\nDone. Patch pushed to GitHub: {game_tree_url(repo, branch, slug)}")
    finally:
        shutil.rmtree(build, ignore_errors=True)
        if not any((PROJECT / ".patch_build").glob("*")):
            shutil.rmtree(PROJECT / ".patch_build", ignore_errors=True)


def patch_remote_game(cfg: dict) -> None:
    repo = str(cfg["repo"]).strip()
    branch = str(cfg.get("branch") or "main").strip()
    max_file_mb = int(cfg.get("max_file_mb", DEFAULT_MAX_FILE_MB))
    batch_push_mb = int(cfg.get("batch_push_mb", DEFAULT_BATCH_PUSH_MB))
    if not re.match(r"^(https://github\.com/|git@github\.com:)[^/]+/[^/]+", repo):
        print("Invalid GitHub URL. Open Settings.")
        return
    if repo.startswith("https://") and not os.environ.get("GITHUB_TOKEN"):
        print("GitHub token is missing. Open Settings and add a token with Contents: Read and write access.")
        return

    games = list_remote_games(repo, branch)
    slug = choose_remote_game(games, "Game to complete / patch")
    if not slug:
        return
    show_header("Update a game")
    item = _find_local_game(slug)
    if item:
        _compare_and_upload_local(repo, branch, slug, item, max_file_mb, batch_push_mb)
    else:
        _patch_from_path(repo, branch, slug, max_file_mb, batch_push_mb)


def delete_remote_game(cfg: dict) -> None:
    repo, branch = str(cfg["repo"]).strip(), str(cfg.get("branch") or "main").strip()
    games = list_remote_games(repo, branch)
    selected = choose_remote_game(games, "Delete a game")
    if not selected:
        return
    slug = selected
    if slug in ("", ".", "..") or "/" in slug or "\\" in slug:
        raise RuntimeError("The GitHub folder name is not safe to delete.")
    show_header("Delete a game", f"Confirm: {slug}")
    print(f"This will remove games/{slug} from GitHub (local files are kept).")
    confirm = input(f"\nType DELETE to confirm (anything else cancels): ").strip()
    if confirm != "DELETE":
        print("Delete cancelled.")
        return

    branch_ref = urllib.parse.quote(f"heads/{branch}", safe="/")
    ref_data = github_api(repo, "GET", f"git/ref/{branch_ref}")
    parent_sha = ref_data["object"]["sha"]
    parent_commit = github_api(repo, "GET", f"git/commits/{parent_sha}")
    base_tree_sha = parent_commit["tree"]["sha"]
    tree_data = github_api(repo, "GET", f"git/trees/{base_tree_sha}?recursive=1")
    if tree_data.get("truncated"):
        raise RuntimeError(
            "GitHub returned a truncated file list because the repository is too large. "
            "Delete was stopped to protect other game files."
        )
    prefix = f"games/{slug}/"
    paths = [
        entry for entry in tree_data.get("tree", [])
        if entry.get("type") == "blob" and str(entry.get("path", "")).startswith(prefix)
    ]
    if not paths:
        print("No files found in that game folder.")
        return

    delete_entries = [
        {"path": entry["path"], "mode": entry.get("mode", "100644"), "type": "blob", "sha": None}
        for entry in paths
    ]
    new_tree = github_api(repo, "POST", "git/trees", {"base_tree": base_tree_sha, "tree": delete_entries})
    new_commit = github_api(repo, "POST", "git/commits", {
        "message": f"Delete game: {selected}",
        "tree": new_tree["sha"],
        "parents": [parent_sha],
    })
    branch_name = urllib.parse.quote(branch, safe="")
    github_api(repo, "PATCH", f"git/refs/heads/{branch_name}", {"sha": new_commit["sha"], "force": False})
    local_copy = REMOTE_WORKTREE / "games" / slug
    if local_copy.is_dir():
        shutil.rmtree(local_copy)
    print(f"Deleted games/{slug} ({len(paths)} files) from GitHub. Local files were kept.")


def read_config() -> dict:
    defaults = {
        "repo": "https://github.com/",
        "branch": "main",
        "max_file_mb": DEFAULT_MAX_FILE_MB,
        "batch_push_mb": DEFAULT_BATCH_PUSH_MB,
        "debug": False,
    }
    try:
        saved = json.loads(CFG.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            defaults.update(saved)
    except (OSError, ValueError):
        pass
    return defaults


def save_config(cfg: dict) -> None:
    CFG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def _pick_numbers(labels: list[str], multi: bool) -> list[int]:
    while True:
        clear_screen()
        show_banner()
        for number, label in enumerate(labels, 1):
            print(f"  {number}. {label}")
        if multi:
            print("  A. All")
        print("  0. Back")
        raw = input("> ").strip().lower()
        if not raw or raw == "0":
            return []
        if multi and raw in ("a", "all"):
            return list(range(len(labels)))
        picked: list[int] = []
        try:
            for token in raw.split(",") if multi else [raw]:
                index = int(token.strip())
                if not 1 <= index <= len(labels):
                    raise ValueError
                if index - 1 not in picked:
                    picked.append(index - 1)
        except ValueError:
            continue
        return picked


def choose_local_games(items: list[GameItem]) -> list[GameItem]:
    width = max(len(item.name) for item in items)
    labels = [f"{item.name.ljust(width)}   {fmt(item.size)}" for item in items]
    return [items[i] for i in _pick_numbers(labels, multi=True)]


def run_upload(selected: list[GameItem], repo: str, branch: str, max_file_mb: int, batch_push_mb: int, resumed: bool = False) -> None:
    started = time.monotonic()
    show_header("Upload games", "Resuming previous upload" if resumed else f"Uploading {len(selected)} game(s)")
    SESSION.start(repo, branch, max_file_mb, batch_push_mb, [item.name for item in selected], resumed=resumed)

    print("\n> Prepare")
    prepared_games: list[PreparedGame] = []
    skipped: list[str] = []
    for number, item in enumerate(selected, 1):
        print(f"\n  {item.name}")
        SESSION.update(phase="prepare", current_game=item.name, detail=f"preparing game {number}/{len(selected)}")
        try:
            prep = prepare_game(item, max_file_mb=max_file_mb, reuse_existing=True)
            _check_github_file_limits(prep.output, max_file_mb)
            prepared_games.append(prep)
            SESSION.update(prepared=[p.item.name for p in prepared_games])
        except Exception as exc:
            log("EXC", traceback.format_exc())
            skipped.append(item.name)
            SESSION.update(skipped=skipped)
            print(f"\nError preparing {item.name}: {type(exc).__name__}: {exc}")
            print("This game was skipped. Other selected games will still be processed.")

    upload_error = ""
    if prepared_games:
        print("\n> Upload")
        SESSION.update(phase="upload", current_game="", detail="connecting to GitHub")
        try:
            upload_games_batch(
                prepared_games,
                repo=repo,
                branch=branch,
                max_file_mb=max_file_mb,
                batch_push_mb=batch_push_mb,
            )
        except Exception as exc:
            log("EXC", traceback.format_exc())
            upload_error = f"{type(exc).__name__}: {exc}"
            ProgressBar.release()
            print(f"\n  Error: {next((ln.strip() for ln in upload_error.splitlines() if ln.strip()), upload_error)[:200]}")
            SESSION.interrupt("failed", upload_error)
    else:
        upload_error = "no game could be prepared"
        SESSION.interrupt("failed", upload_error)

    if not upload_error:
        SESSION.finish()

    elapsed = fmt_time(time.monotonic() - started)
    if upload_error:
        print(f"\n  Failed after {elapsed}. Run again to continue.")
        log("ERROR", upload_error)
    else:
        print(f"\n  Done in {elapsed}")
    if skipped:
        print("  Skipped: " + ", ".join(skipped))
    pause()


def upload_menu(cfg: dict) -> None:
    items, warnings = discover_games(GAMES)
    log("SCAN", f"{len(items)} game(s) found in {GAMES}; warnings={warnings}")
    for warning in warnings:
        print(f"Warning: {warning}")
    if not items:
        print("No games found. Put game folders or PFS files in games/.")
        pause()
        return
    selected = choose_local_games(items)
    if not selected:
        return

    repo = str(cfg["repo"]).strip()
    branch = str(cfg.get("branch") or "main").strip()
    max_file_mb = int(cfg.get("max_file_mb", DEFAULT_MAX_FILE_MB))
    batch_push_mb = int(cfg.get("batch_push_mb", DEFAULT_BATCH_PUSH_MB))
    has_token = bool(os.environ.get("GITHUB_TOKEN"))

    show_header("Upload games", "Confirm")
    print("Input summary")
    print(f"  Repo        : {repo}")
    print(f"  Branch      : {branch}")
    print(f"  Max file    : {max_file_mb} MB (larger files are split into .bin parts)")
    print(f"  Batch push  : {batch_push_mb} MB")
    print(f"  Token       : {'set (hidden)' if has_token else 'MISSING'}")
    print("  Games       :")
    for item in selected:
        print(f"    - {item.name} ({fmt(item.size)}, {len(item.files)} files)")
    log("INPUT", f"repo={repo} branch={branch} max_file_mb={max_file_mb} batch_push_mb={batch_push_mb} "
                 f"token={'set' if has_token else 'missing'} games={[i.name for i in selected]}")

    if not re.match(r"^(https://github\.com/|git@github\.com:)[^/]+/[^/]+", repo):
        print("\nInvalid GitHub URL. Open Settings.")
        pause()
        return
    if repo.startswith("https://") and not has_token:
        print("\nGitHub token is missing. Open Settings and add a token with Contents: Read and write access.")
        pause()
        return
    if not ask_yes_no("Extract and upload?", default=True):
        return

    run_upload(selected, repo, branch, max_file_mb, batch_push_mb)


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
        if pid <= 0 or pid == os.getpid():
            return False
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, errors="replace").stdout.lower()
            return "python" in out
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def offer_resume(cfg: dict) -> None:
    data = Session.load()
    if not data:
        return
    if data.get("status") == "running" and _pid_alive(data.get("pid")):
        log("SESSION", f"another window (pid {data.get('pid')}) is still running this upload; not offering resume")
        return

    status = str(data.get("status", "unknown"))
    reasons = {
        "running": "The window was closed suddenly or the program crashed",
        "closed": "The window or program was closed before it finished",
        "cancelled": "It was cancelled with Ctrl+C",
        "failed": "It stopped because of an error",
    }
    where = str(data.get("phase", "?"))
    if data.get("current_game"):
        where += f" | game: {data['current_game']}"
    if data.get("batches"):
        where += f" | batch {data.get('batch', 0)}/{data['batches']} ({data.get('batches_done', 0)} finished)"
    if data.get("detail"):
        where += f" | {data['detail']}"

    show_header("Resume previous upload")
    print("An unfinished upload was found.\n")
    print(f"  Save file    : {SESSION_FILE}")
    print(f"  What happened: {reasons.get(status, status)}")
    print(f"  Started      : {data.get('started_at')}")
    print(f"  Last update  : {data.get('updated_at')}")
    print(f"  Stopped at   : {where}")
    print(f"  Games        : {', '.join(data.get('games') or [])}")
    if data.get("error"):
        print(f"  Last error   : {str(data['error']).splitlines()[0][:200]}")
    if data.get("push_record"):
        print(f"  Push record  : {data['push_record']}")
    print("\nOn resume, finished extractions are reused and files already on GitHub are skipped.")

    if not ask_yes_no("Resume where it stopped?", default=True):
        if ask_yes_no("Delete the save file?", default=False):
            Session.delete()
            print("Save file deleted.")
            pause()
        return

    wanted = list(data.get("games") or [])
    items, _warnings = discover_games(GAMES)
    selected = [item for item in items if item.name in wanted]
    missing = [name for name in wanted if name not in {item.name for item in selected}]
    if missing:
        print(f"\nNot found in games/: {', '.join(missing)}")
    if not selected:
        print("Nothing to resume. The games are no longer in the games/ folder.")
        if ask_yes_no("Delete the save file?", default=True):
            Session.delete()
        return

    repo = str(data.get("repo") or cfg.get("repo") or "").strip()
    branch = str(data.get("branch") or cfg.get("branch") or "main").strip()
    max_file_mb = int(data.get("max_file_mb") or cfg.get("max_file_mb", DEFAULT_MAX_FILE_MB))
    batch_push_mb = int(data.get("batch_push_mb") or cfg.get("batch_push_mb", DEFAULT_BATCH_PUSH_MB))
    if repo.startswith("https://") and not os.environ.get("GITHUB_TOKEN"):
        print("\nGitHub token is missing. Open Settings and add a token, then start again.")
        pause()
        return
    log("SESSION", f"resuming: games={[i.name for i in selected]} stopped_at={where}")
    run_upload(selected, repo, branch, max_file_mb, batch_push_mb, resumed=True)


def show_banner() -> None:
    letters = {
        "A": [" ### ", "#   #", "#####", "#   #", "#   #"],
        "R": ["#### ", "#   #", "#### ", "# #  ", "#  ##"],
        "T": ["#####", "  #  ", "  #  ", "  #  ", "  #  "],
        "W": ["#   #", "#   #", "# # #", "## ##", "#   #"],
        "E": ["#####", "#    ", "#### ", "#    ", "#####"],
        "B": ["#### ", "#   #", "#### ", "#   #", "#### "],
        "S": [" ####", "#    ", " ### ", "    #", "#### "],
    }
    print()
    for row in range(5):
        print("    " + "  ".join(letters[letter][row] for letter in "ARTWEBS"))
    print("                      NewGen\n")


def view_log(lines: int = 60) -> None:
    if not LOG_FILE.is_file():
        print("[LOG] No log yet")
        return
    text = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    print(f"[LOG] File: {LOG_FILE}")
    print(f"[LOG] Last {min(lines, len(text))} of {len(text)} lines\n")
    for line in text[-lines:]:
        print(re.sub(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+ ", "", line)[:300])


def terminal_menu() -> int:
    load_local_env()
    cfg = read_config()
    set_debug(DEBUG or bool(cfg.get("debug")))
    log("START", f"python={platform.python_version()} os={platform.platform()} project={PROJECT} debug={DEBUG}")
    _install_exit_hooks()
    try:
        offer_resume(cfg)
    except KeyboardInterrupt:
        SESSION.interrupt("cancelled", "Ctrl+C")
        print("\nCancelled.")
        pause()
    except Exception as exc:
        log("EXC", traceback.format_exc())
        SESSION.interrupt("failed", f"{type(exc).__name__}: {exc}")
        print(f"\nError: {type(exc).__name__}: {exc}")
        print(f"Details saved to: {LOG_FILE}")
        pause()
    titles = {
        "1": "Upload games",
        "2": "Update a game",
        "3": "View games",
        "4": "Delete a game",
        "5": "Settings",
    }
    while True:
        clear_screen()
        show_banner()
        print("  1. Upload games")
        print("  2. Update a game")
        print("  3. View games")
        print("  4. Delete a game")
        print("  5. Settings")
        print("  0. Exit")
        choice = input("> ").strip()
        if choice == "0":
            log("EXIT", "user exited")
            return 0
        if choice not in titles:
            continue
        show_header(titles[choice])
        try:
            if choice == "1":
                upload_menu(cfg)
            elif choice == "2":
                patch_remote_game(cfg)
                pause()
            elif choice == "3":
                view_games(cfg)
                pause()
            elif choice == "4":
                delete_remote_game(cfg)
                pause()
            elif choice == "5":
                cfg = settings_menu(cfg)
        except KeyboardInterrupt:
            ProgressBar.release()
            log("CANCEL", f"KeyboardInterrupt in '{titles[choice]}'")
            SESSION.interrupt("cancelled", "Ctrl+C")
            print("\nCancelled.")
            if SESSION_FILE.exists():
                print("Progress saved. Start the script again to continue.")
            pause()
        except Exception as exc:
            ProgressBar.release()
            log("EXC", traceback.format_exc())
            SESSION.interrupt("failed", f"{type(exc).__name__}: {exc}")
            print(f"\n  Error: {str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__}")
            if DEBUG:
                print(mask_secret(traceback.format_exc()))
            print(f"Details saved to: {LOG_FILE}")
            pause()


def _ask_int(prompt: str, low: int, high: int) -> int | None:
    value = input(f"{prompt} ({low}-{high}): ").strip()
    if not value:
        return None
    try:
        number = int(value)
    except ValueError:
        number = low - 1
    if not low <= number <= high:
        print(f"Enter a number from {low} to {high}.")
        pause()
        return None
    return number


def settings_menu(cfg: dict) -> dict:
    while True:
        clear_screen()
        show_banner()
        token = "set" if os.environ.get("GITHUB_TOKEN") else "missing"
        print(f"  1. GitHub URL      : {cfg.get('repo')}")
        print(f"  2. Branch          : {cfg.get('branch')}")
        print(f"  3. Max file (MB)   : {cfg.get('max_file_mb')}")
        print(f"  4. Batch push (MB) : {cfg.get('batch_push_mb')}")
        print(f"  5. GitHub token    : {token}")
        print(f"  6. Debug mode      : {'ON' if DEBUG else 'OFF'}")
        print("  7. Debug log")
        print("  0. Back")
        choice = input("> ").strip()
        if choice == "0":
            return cfg
        if choice == "1":
            value = input("GitHub URL: ").strip()
            if value:
                cfg["repo"] = value
        elif choice == "2":
            value = input("Branch: ").strip()
            if value:
                cfg["branch"] = value
        elif choice == "3":
            number = _ask_int("Max file size in MB before .bin split", 1, 99)
            if number is not None:
                cfg["max_file_mb"] = number
        elif choice == "4":
            number = _ask_int("Batch push size in MB", 50, 2000)
            if number is not None:
                cfg["batch_push_mb"] = number
        elif choice == "5":
            value = input("GitHub token: ").strip()
            if value:
                ENV_FILE.write_text(f"GITHUB_TOKEN={value}\n", encoding="utf-8")
                try:
                    os.chmod(ENV_FILE, 0o600)
                except OSError:
                    pass
                os.environ["GITHUB_TOKEN"] = value
        elif choice == "6":
            set_debug(not DEBUG)
            cfg["debug"] = DEBUG
        elif choice == "7":
            show_header("Debug log")
            view_log()
            pause()
        else:
            continue
        save_config(cfg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract Artemis PFS archives and upload game files to GitHub")
    parser.add_argument("--scan", action="store_true", help="scan local games/ without uploading")
    parser.add_argument("--prepare", metavar="GAME_NAME", help="extract/copy one game into build/ without uploading")
    parser.add_argument("--debug", action="store_true", help="print every command/result/step on screen (always logged to logs/)")
    args = parser.parse_args(argv)
    if args.debug:
        set_debug(True)
    cfg = read_config()
    max_file_mb = int(cfg.get("max_file_mb", DEFAULT_MAX_FILE_MB))
    if args.scan:
        items, warnings = discover_games(GAMES)
        for warning in warnings:
            print(f"Warning: {warning}")
        for index, item in enumerate(items, 1):
            print(f"{index}. {item.name}: {fmt(item.size)} / {len(item.files)} source files")
        return 0
    if args.prepare:
        items, _ = discover_games(GAMES)
        item = next((g for g in items if g.name.casefold() == args.prepare.casefold()), None)
        if not item:
            raise RuntimeError(f"Game not found: {args.prepare}")
        prepare_game(item, max_file_mb=max_file_mb, reuse_existing=False)
        return 0
    return terminal_menu()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(130)
    except Exception as exc:
        log("EXC", traceback.format_exc())
        print(f"Error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"Details saved to: {LOG_FILE}", file=sys.stderr)
        raise SystemExit(1)