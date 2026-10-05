import json
import tempfile
import unittest
from pathlib import Path

import supervisor as sv


class FakeProcess:
    _next_pid = 1000

    def __init__(self):
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class Harness:
    """Drives a Supervisor with a fake clock, fake processes and scripted health."""

    def __init__(self, tmp: Path):
        self.now = 1000.0
        self.healthy = {}      # service name -> bool
        self.open_ports = set()
        self.port_owners = {}  # port -> (pid, image name)
        self.spawned = []      # (service name, FakeProcess)
        self.killed = []
        self.status_file = tmp / "status.json"
        self.stop_file = tmp / "supervisor.stop"
        self.services = [
            sv.Service("spoolman", ["spoolman"], tmp, {}, "http://127.0.0.1:7912/api/v1/health", 7912, tmp / "sm.log"),
            sv.Service("spooler", ["spooler"], tmp, {}, "http://127.0.0.1:8080/api/health", 8080, tmp / "sp.log"),
        ]
        self.sup = sv.Supervisor(
            self.services, self.status_file, self.stop_file,
            clock=lambda: self.now,
            probe=self.probe,
            is_port_open=lambda host, port: port in self.open_ports,
            spawn=self.spawn,
            kill=self.kill,
            port_owner=lambda port: self.port_owners.get(port, (None, None))[0],
            image_of=lambda pid: next((img for p, img in self.port_owners.values() if p == pid), None),
        )

    def probe(self, url, timeout=None):
        name = next(s.name for s in self.services if s.health_url == url)
        ok = self.healthy.get(name, False)
        return ok, None if ok else "connection refused"

    def spawn(self, svc):
        proc = FakeProcess()
        self.spawned.append((svc.name, proc))
        return proc

    def kill(self, pid):
        self.killed.append(pid)
        for port, (owner, _) in list(self.port_owners.items()):
            if owner == pid:
                del self.port_owners[port]
                self.open_ports.discard(port)

    def tick(self, advance=0.0):
        self.now += advance
        self.sup.tick()

    def svc(self, name):
        return next(s for s in self.services if s.name == name)

    def spawned_names(self):
        return [name for name, _ in self.spawned]


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.h = Harness(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_starts_services_that_are_not_running(self):
        self.h.tick()

        self.assertEqual(self.h.spawned_names(), ["spoolman", "spooler"])
        status = json.loads(self.h.status_file.read_text())
        self.assertEqual([s["name"] for s in status["services"]], ["spoolman", "spooler"])

    def test_adopts_instances_that_are_already_listening(self):
        self.h.open_ports = {7912, 8080}
        self.h.healthy = {"spoolman": True, "spooler": True}

        self.h.tick()
        self.h.tick(10)

        self.assertEqual(self.h.spawned, [])
        self.assertTrue(all(s.adopted and s.healthy for s in self.h.services))

    def test_crashed_process_is_restarted_after_a_backoff(self):
        self.h.tick()
        self.h.healthy = {"spoolman": True, "spooler": True}
        self.h.tick(10)

        self.h.svc("spooler").process.returncode = 1
        self.h.tick(10)
        self.assertEqual(len(self.h.spawned), 2)  # waiting out the backoff

        self.h.tick(6)
        self.assertEqual(self.h.spawned_names(), ["spoolman", "spooler", "spooler"])
        self.assertEqual(self.h.svc("spooler").restarts, 1)

    def test_backoff_doubles_up_to_the_cap(self):
        self.assertEqual([sv.Supervisor.backoff(n) for n in (1, 2, 3)], [5, 10, 20])
        self.assertEqual(sv.Supervisor.backoff(20), sv.RESTART_BACKOFF_MAX)

    def test_unhealthy_process_is_restarted_after_repeated_failed_checks(self):
        self.h.tick()
        self.h.healthy = {"spoolman": True, "spooler": True}
        self.h.tick(10)
        spooler_pid = self.h.svc("spooler").process.pid

        self.h.healthy["spooler"] = False
        self.h.tick(10)
        self.h.tick(10)
        self.assertEqual(self.h.killed, [])

        self.h.tick(10)
        self.assertEqual(self.h.killed, [spooler_pid])

        self.h.tick(6)
        self.assertEqual(self.h.spawned_names(), ["spoolman", "spooler", "spooler"])

    def test_new_process_gets_a_startup_grace_period(self):
        self.h.tick()
        for _ in range(8):
            self.h.tick(10)
        self.assertEqual(self.h.killed, [])

        self.h.tick(10)  # 90 s after start
        self.assertEqual(len(self.h.killed), 2)

    def test_unhealthy_adopted_python_instance_is_replaced(self):
        self.h.healthy = {"spoolman": True}
        self.h.open_ports = {7912, 8080}
        self.h.port_owners = {8080: (4242, "python.exe")}
        self.h.tick()
        for _ in range(9):
            self.h.tick(10)
        self.assertEqual(self.h.killed, [4242])

        self.h.tick(10)
        self.assertEqual(self.h.spawned_names(), ["spooler"])

    def test_port_held_by_another_program_is_never_killed(self):
        self.h.healthy = {"spoolman": True}
        self.h.open_ports = {7912, 8080}
        self.h.port_owners = {8080: (4242, "nginx.exe")}
        self.h.tick()
        for _ in range(12):
            self.h.tick(10)

        self.assertNotIn(4242, self.h.killed)
        self.assertIn("nginx.exe", self.h.svc("spooler").last_error)

    def test_stop_request_shuts_down_services_and_clears_status(self):
        self.h.tick()
        pids = [self.h.svc(name).process.pid for name in ("spooler", "spoolman")]
        self.h.stop_file.touch()

        self.h.sup.run(interval=0, sleep=lambda _: None)

        self.assertEqual(self.h.killed, pids)  # Spooler first, then Spoolman
        self.assertFalse(self.h.status_file.exists())
        self.assertFalse(self.h.stop_file.exists())


class HelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_parse_netstat_finds_the_listening_pid_for_an_exact_port(self):
        output = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:18080          0.0.0.0:0              LISTENING       555
  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       10124
  TCP    127.0.0.1:7912         127.0.0.1:57147        ESTABLISHED     20764
  TCP    127.0.0.1:7912         0.0.0.0:0              LISTENING       20764
"""
        self.assertEqual(sv.parse_netstat_listen_pid(output, 8080), 10124)
        self.assertEqual(sv.parse_netstat_listen_pid(output, 7912), 20764)
        self.assertIsNone(sv.parse_netstat_listen_pid(output, 80))

    def test_read_env_file(self):
        env = self.dir / ".env"
        env.write_text("# comment\n\nA=1\nB = two=2\n  # indented comment\nnot a pair\n")
        self.assertEqual(sv.read_env_file(env), {"A": "1", "B": "two=2"})
        self.assertEqual(sv.read_env_file(self.dir / "missing.env"), {})

    def test_rotate_log_shifts_old_files(self):
        log = self.dir / "service.log"
        for generation in ("first", "second"):
            log.write_text(generation * 10)
            sv.rotate_log(log, max_bytes=10, backups=2)

        self.assertFalse(log.exists())
        self.assertEqual((self.dir / "service.log.1").read_text(), "second" * 10)
        self.assertEqual((self.dir / "service.log.2").read_text(), "first" * 10)

    def test_build_services_reads_both_projects_env_files(self):
        app = self.dir / "spooler"
        spoolman_dir = self.dir / "spoolman"
        app.mkdir()
        spoolman_dir.mkdir()
        (app / ".env").write_text(
            f"DATA_DIR={self.dir / 'data'}\nHTTP_PORT=9090\nSPOOLMAN_SERVICE_DIR={spoolman_dir}\n")
        (spoolman_dir / ".env").write_text("SPOOLMAN_HOST=0.0.0.0\nSPOOLMAN_PORT=7999\n")

        (spoolman, spooler), data_dir = sv.build_services(app, base_env={"PATH": ""})

        self.assertEqual(data_dir, self.dir / "data")
        self.assertEqual(spoolman.health_url, "http://127.0.0.1:7999/api/v1/health")
        self.assertEqual(spoolman.env["SPOOLMAN_DIR_DATA"], str(spoolman_dir.resolve() / "data"))
        self.assertEqual((spooler.port, spooler.health_url), (9090, "http://127.0.0.1:9090/api/health"))

    def test_instance_lock_is_exclusive(self):
        first = sv.InstanceLock(self.dir / "supervisor.lock")
        second = sv.InstanceLock(self.dir / "supervisor.lock")
        self.assertTrue(first.acquire())
        try:
            self.assertFalse(second.acquire())
        finally:
            first.release()
        self.assertTrue(second.acquire())
        second.release()


if __name__ == "__main__":
    unittest.main()
