#!/usr/bin/env python3
"""
Spooler supervisor – keeps Spoolman and Spooler running on a workstation.

Starts both services, probes their health endpoints, and restarts a service
that exits or stops answering. Instances that are already running (for
example ones started by hand) are adopted rather than duplicated.

  pythonw supervisor.py            run in the background (used by autostart)
  python  supervisor.py --wait 90  block until both services are healthy
  python  supervisor.py --stop     stop the supervisor and both services
  python  supervisor.py --status   print the last recorded service status
"""

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

CHECK_INTERVAL_S    = 10
HEALTH_TIMEOUT_S    = 5
UNHEALTHY_THRESHOLD = 3      # consecutive failed probes before a restart
STARTUP_GRACE_S     = 90     # time a new or adopted process gets to become healthy
STABLE_AFTER_S      = 120    # healthy this long after a start resets the crash backoff
RESTART_BACKOFF_S   = 5
RESTART_BACKOFF_MAX = 300
LOG_MAX_BYTES       = 5 * 1024 * 1024
LOG_BACKUPS         = 3

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

log = logging.getLogger("supervisor")
log.addHandler(logging.NullHandler())

# Health probes always target this machine; never route them through a proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ── Configuration ──────────────────────────────────────────────────────────────

def read_env_file(path: Path) -> dict:
    """Parse KEY=value lines using the same rules as server.py's .env loader."""
    values: dict = {}
    try:
        text = path.read_text()
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip():
            values[key.strip()] = val.strip()
    return values


def venv_python(project_dir: Path) -> Path:
    if os.name == "nt":
        return project_dir / ".venv" / "Scripts" / "python.exe"
    return project_dir / ".venv" / "bin" / "python"


@dataclass
class Service:
    name: str
    command: list
    cwd: Path
    env: dict
    health_url: str
    port: int
    log_file: Path
    process: object = None
    adopted: bool = False
    started_at: float | None = None
    healthy: bool = False
    healthy_since_start: bool = False
    failures: int = 0          # consecutive failed health probes
    crash_streak: int = 0      # restarts without a stable healthy period in between
    restarts: int = 0
    next_start_at: float = 0.0
    last_error: str | None = None
    last_healthy_at: float | None = None

    @property
    def host(self) -> str:
        return urllib.parse.urlparse(self.health_url).hostname or "127.0.0.1"

    def public(self, now: float) -> dict:
        idle = self.process is None and not self.adopted
        return {
            "name":            self.name,
            "pid":             self.process.pid if self.process is not None else None,
            "adopted":         self.adopted,
            "running":         not idle,
            "healthy":         self.healthy,
            "started_at":      self.started_at,
            "last_healthy_at": self.last_healthy_at,
            "restarts":        self.restarts,
            "failures":        self.failures,
            "last_error":      self.last_error,
            "next_start_in_s": max(0, round(self.next_start_at - now)) if idle else None,
        }


def build_services(app_dir: Path = APP_DIR, base_env: dict | None = None) -> tuple:
    """Return ([spoolman, spooler], spooler_data_dir) from the two projects' .env files."""
    base_env = dict(os.environ if base_env is None else base_env)
    spooler_cfg = read_env_file(app_dir / ".env")

    def setting(key: str, default: str = "") -> str:
        return base_env.get(key) or spooler_cfg.get(key) or default

    data_dir = Path(setting("DATA_DIR", str(app_dir)))
    log_dir = data_dir / "logs"
    spoolman_dir = Path(setting("SPOOLMAN_SERVICE_DIR", str(app_dir.parent / "spoolman-service"))).resolve()

    sm_cfg = read_env_file(spoolman_dir / ".env")
    sm_host = sm_cfg.get("SPOOLMAN_HOST", "127.0.0.1")
    sm_port = int(sm_cfg.get("SPOOLMAN_PORT", "7912"))
    sm_env = {**base_env, **sm_cfg}
    sm_env.setdefault("SPOOLMAN_DIR_DATA", str(spoolman_dir / "data"))
    sm_env.update(SPOOLMAN_HOST=sm_host, SPOOLMAN_PORT=str(sm_port))
    sm_env["PATH"] = f"{venv_python(spoolman_dir).parent}{os.pathsep}{base_env.get('PATH', '')}"
    probe_host = "127.0.0.1" if sm_host in ("", "0.0.0.0") else sm_host
    spoolman = Service(
        name="spoolman",
        command=[str(venv_python(spoolman_dir)), "-m", "uvicorn", "spoolman.main:app",
                 "--host", sm_host, "--port", str(sm_port)],
        cwd=spoolman_dir,
        env=sm_env,
        health_url=f"http://{probe_host}:{sm_port}/api/v1/health",
        port=sm_port,
        log_file=log_dir / "spoolman.log",
    )

    http_port = int(setting("HTTP_PORT", "8080"))
    spooler = Service(
        name="spooler",
        command=[str(venv_python(app_dir)), "server.py"],
        cwd=app_dir,
        env={**base_env, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
        health_url=f"http://127.0.0.1:{http_port}/api/health",
        port=http_port,
        log_file=log_dir / "spooler.log",
    )
    return [spoolman, spooler], data_dir


# ── Process helpers ────────────────────────────────────────────────────────────

def probe_health(url: str, timeout: float = HEALTH_TIMEOUT_S) -> tuple:
    try:
        with _opener.open(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300, None
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e) or type(e).__name__


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def parse_netstat_listen_pid(output: str, port: int) -> int | None:
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].rsplit(":", 1)[-1] == str(port):
                try:
                    return int(parts[4])
                except ValueError:
                    continue
    return None


def listening_pid(port: int) -> int | None:
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             timeout=15, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_netstat_listen_pid(out, port)


def process_image(pid: int) -> str | None:
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=NO_WINDOW).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out.startswith('"'):
        return None  # "INFO: No tasks are running which match the specified criteria."
    return out.split('","', 1)[0].strip('"')


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       timeout=30, creationflags=NO_WINDOW)
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass


def rotate_log(path: Path, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS) -> None:
    """Shift path → path.1 → path.2 … when it is larger than max_bytes."""
    try:
        if path.stat().st_size < max_bytes:
            return
        for i in range(backups - 1, 0, -1):
            src = path.with_name(f"{path.name}.{i}")
            if src.exists():
                os.replace(src, path.with_name(f"{path.name}.{i + 1}"))
        os.replace(path, path.with_name(f"{path.name}.1"))
    except OSError:
        pass  # missing, or still held open by a process we don't own


def spawn(svc: Service):
    svc.log_file.parent.mkdir(parents=True, exist_ok=True)
    rotate_log(svc.log_file)
    with open(svc.log_file, "ab") as fh:
        fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} supervisor starting {svc.name} =====\n".encode())
        fh.flush()
        return subprocess.Popen(
            svc.command,
            cwd=svc.cwd,
            env=svc.env,
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW | NEW_GROUP,
            start_new_session=os.name != "nt",
        )


# ── Supervisor ─────────────────────────────────────────────────────────────────

class Supervisor:
    def __init__(
        self,
        services: list,
        status_file: Path,
        stop_file: Path,
        *,
        clock=time.time,
        probe=probe_health,
        is_port_open=port_open,
        spawn=spawn,
        kill=kill_tree,
        port_owner=listening_pid,
        image_of=process_image,
    ):
        self.services     = services
        self.status_file  = status_file
        self.stop_file    = stop_file
        self.clock        = clock
        self.probe        = probe
        self.is_port_open = is_port_open
        self.spawn        = spawn
        self.kill         = kill
        self.port_owner   = port_owner
        self.image_of     = image_of
        self.started_at   = clock()

    @staticmethod
    def backoff(streak: int) -> float:
        return min(RESTART_BACKOFF_MAX, RESTART_BACKOFF_S * 2 ** max(0, streak - 1))

    def tick(self) -> None:
        now = self.clock()
        for svc in self.services:
            try:
                self.check(svc, now)
            except Exception as e:  # one service's problem must not stop supervision of the other
                svc.last_error = f"supervisor error: {e}"
                log.exception("Error while checking %s", svc.name)
        self.write_status(now)

    def check(self, svc: Service, now: float) -> None:
        if svc.process is not None and svc.process.poll() is not None:
            self.on_exit(svc, now, svc.process.returncode)

        if svc.process is None and not svc.adopted:
            if now < svc.next_start_at:
                return
            if self.is_port_open(svc.host, svc.port):
                self.adopt(svc, now)
            else:
                self.start(svc, now)
            return

        ok, err = self.probe(svc.health_url)
        if ok:
            if not svc.healthy:
                log.info("%s is healthy", svc.name)
            svc.healthy = True
            svc.healthy_since_start = True
            svc.failures = 0
            svc.last_error = None
            svc.last_healthy_at = now
            if svc.started_at is not None and now - svc.started_at >= STABLE_AFTER_S:
                svc.crash_streak = 0
            return

        svc.failures += 1
        svc.last_error = err
        if svc.healthy:
            log.warning("%s health check failed: %s", svc.name, err)
        svc.healthy = False
        starting = (not svc.healthy_since_start and svc.started_at is not None
                    and now - svc.started_at < STARTUP_GRACE_S)
        if starting or svc.failures < UNHEALTHY_THRESHOLD:
            return
        self.restart(svc, now, f"unhealthy after {svc.failures} failed checks ({err})")

    def on_exit(self, svc: Service, now: float, code) -> None:
        svc.process = None
        svc.healthy = False
        svc.crash_streak += 1
        svc.next_start_at = now + self.backoff(svc.crash_streak)
        svc.last_error = f"exited with code {code}"
        log.warning("%s exited with code %s; restarting in %.0f s", svc.name, code, svc.next_start_at - now)

    def adopt(self, svc: Service, now: float) -> None:
        svc.adopted = True
        svc.started_at = now
        svc.healthy = False
        svc.healthy_since_start = False
        svc.failures = 0
        log.info("%s is already listening on port %s; supervising the existing instance", svc.name, svc.port)

    def start(self, svc: Service, now: float) -> None:
        if svc.started_at is not None:
            svc.restarts += 1
        try:
            svc.process = self.spawn(svc)
        except OSError as e:
            svc.process = None
            svc.crash_streak += 1
            svc.next_start_at = now + self.backoff(svc.crash_streak)
            svc.last_error = f"could not start: {e}"
            log.error("Could not start %s: %s", svc.name, e)
            return
        svc.adopted = False
        svc.started_at = now
        svc.healthy = False
        svc.healthy_since_start = False
        svc.failures = 0
        log.info("Started %s (pid %s)", svc.name, svc.process.pid)

    def stop_service(self, svc: Service) -> bool:
        """Terminate the service. Returns False if its port is held by something we won't kill."""
        if svc.process is not None:
            self.kill(svc.process.pid)
            try:
                svc.process.wait(timeout=15)
            except Exception:
                pass
            svc.process = None
        elif svc.adopted:
            pid = self.port_owner(svc.port)
            if pid is not None:
                image = self.image_of(pid) or ""
                if "python" not in image.lower():
                    svc.last_error = f"port {svc.port} is held by {image or f'pid {pid}'}; not stopping it"
                    log.error("Not stopping %s: %s", svc.name, svc.last_error)
                    return False
                self.kill(pid)
        svc.adopted = False
        svc.healthy = False
        return True

    def restart(self, svc: Service, now: float, reason: str) -> None:
        log.warning("Restarting %s: %s", svc.name, reason)
        if not self.stop_service(svc):
            svc.failures = 0  # re-evaluate after another full round of failed checks
            return
        svc.crash_streak += 1
        svc.next_start_at = now + self.backoff(svc.crash_streak)
        svc.last_error = reason

    def write_status(self, now: float) -> None:
        data = {
            "pid":        os.getpid(),
            "started_at": self.started_at,
            "updated_at": now,
            "services":   [svc.public(now) for svc in self.services],
        }
        tmp = self.status_file.with_suffix(".tmp")
        try:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, self.status_file)
        except OSError as e:
            log.warning("Could not write %s: %s", self.status_file.name, e)

    def run(self, interval: float = CHECK_INTERVAL_S, sleep=time.sleep) -> None:
        log.info("Supervisor started (pid %s)", os.getpid())
        try:
            while not self.stop_file.exists():
                self.tick()
                deadline = self.clock() + interval
                while self.clock() < deadline and not self.stop_file.exists():
                    sleep(0.5)
            log.info("Stop requested")
        except KeyboardInterrupt:
            log.info("Interrupted")
        self.shutdown()

    def shutdown(self) -> None:
        for svc in reversed(self.services):
            log.info("Stopping %s", svc.name)
            self.stop_service(svc)
        for path in (self.status_file, self.stop_file):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning("Could not remove %s: %s", path.name, e)
        log.info("Supervisor stopped")


# ── Entry point ────────────────────────────────────────────────────────────────

class InstanceLock:
    """Non-blocking exclusive file lock held for the supervisor's lifetime."""

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None


def wait_healthy(services: list, timeout: float, probe=probe_health,
                 clock=time.monotonic, sleep=time.sleep) -> bool:
    deadline = clock() + timeout
    pending = list(services)
    while True:
        pending = [s for s in pending if not probe(s.health_url, timeout=3)[0]]
        if not pending:
            return True
        if clock() >= deadline:
            return False
        sleep(1)


def configure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handlers: list = [RotatingFileHandler(log_file, maxBytes=LOG_MAX_BYTES,
                                          backupCount=LOG_BACKUPS, encoding="utf-8")]
    if sys.stdout is not None:  # pythonw has no console
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        handlers=handlers)


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Keep Spoolman and Spooler running.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--stop", action="store_true", help="stop the running supervisor and both services")
    group.add_argument("--status", action="store_true", help="print the last recorded service status")
    group.add_argument("--wait", type=float, metavar="SECONDS",
                       help="wait until both services answer their health checks")
    args = parser.parse_args(argv)

    services, data_dir = build_services()
    status_file = data_dir / "supervisor_status.json"
    stop_file   = data_dir / "supervisor.stop"
    lock        = InstanceLock(data_dir / "supervisor.lock")

    if args.status:
        try:
            print(status_file.read_text())
        except FileNotFoundError:
            print("Supervisor is not running.")
            return 1
        return 0

    if args.wait is not None:
        ok = wait_healthy(services, args.wait)
        for svc in services:
            healthy, err = probe_health(svc.health_url, timeout=3)
            base = svc.health_url.split("/api/", 1)[0]
            print(f"{svc.name:9} {'healthy' if healthy else f'NOT HEALTHY ({err})':<24} {base}")
        return 0 if ok else 1

    if args.stop:
        if lock.acquire():
            lock.release()
            print("Supervisor is not running.")
            return 1
        stop_file.touch()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if lock.acquire():
                lock.release()
                print("Supervisor and services stopped.")
                return 0
            time.sleep(0.5)
        print("Timed out waiting for the supervisor to stop.")
        return 1

    if not lock.acquire():
        return 0  # another supervisor already runs; exit cleanly so autostart doesn't retry
    try:
        stop_file.unlink(missing_ok=True)  # stale request from an earlier --stop
        configure_logging(data_dir / "logs" / "supervisor.log")
        Supervisor(services, status_file, stop_file).run()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
