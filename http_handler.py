"""
HTTP request handler: static files + /api/ routes.
"""

import http.client
import json
import re
import ssl
import socket
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import os
import asyncio
import backup
import bed_cleared
import dashboard
import export_plan
import holds
import farm
import health
import jobs
import outcome_prompt
import preflight
import remembered_defaults
import print_history
import projects
import settings_review
import slicer
import splits
import state
import time
import time_estimate
from persistence import DATA_DIR, save_printers
from values import safe_filename
from push import (
    WEBPUSH_AVAILABLE, add_subscription, get_public_key, has_subscriptions,
    load_notif_settings, remove_subscription, save_notif_settings, send_push_all,
)
from spoolman import get_spoolman_db, get_spoolman_url

CAMERA_STALL_TIMEOUT_S  = 15   # close a proxied camera stream after this long without data
CAMERA_REENABLE_SETTLE_S = 1   # CC1 needs a moment after re-enable before /video answers

CERT_FILE = DATA_DIR / "cert.pem"
KEY_FILE  = DATA_DIR / "key.pem"

MAX_BODY = 100 * 1024 * 1024  # 100 MB

PUBLIC_DIR = Path(__file__).parent / "public"


def ui_build() -> str:
    """Fingerprint of the browser UI's own files (not vendored ones): open pages reload when it changes."""
    newest = 0
    for path in PUBLIC_DIR.rglob("*"):
        if path.suffix in (".html", ".js", ".css") and "vendor" not in path.parts:
            newest = max(newest, path.stat().st_mtime_ns)
    return f"{newest:x}"

# When true, Spoolman's web UI is proxied through /spoolman/ on this server.
# When false (default), /spoolman redirects the browser directly to SPOOLMAN_URL.
PROXY_SPOOLMAN = os.getenv("PROXY_SPOOLMAN", "true").lower() in ("1", "true", "yes")


def ensure_ssl_cert() -> bool:
    if CERT_FILE.exists() and KEY_FILE.exists():
        return True
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"
    try:
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(KEY_FILE), "-out", str(CERT_FILE),
                "-days", "3650", "-nodes",
                "-subj", "/CN=spooler.local",
                "-addext", f"subjectAltName=IP:{local_ip},IP:127.0.0.1,DNS:localhost",
            ],
            check=True,
            capture_output=True,
        )
        print(f"[SSL] Certificate generated (IP: {local_ip})")
        return True
    except Exception as e:
        print(f"[SSL] Could not generate certificate: {e}")
        return False


_UTF8_TYPES = {"text/javascript", "application/javascript", "text/css", "application/json", "image/svg+xml", "text/html"}


class SPHandler(SimpleHTTPRequestHandler):
    """Serve static files from ./public and handle /api/ calls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent / "public"), **kwargs)

    def log_message(self, fmt, *args):
        pass  # silence access log

    def guess_type(self, path):
        # Without a charset the browser guesses, and read scripts/styles as Windows-1252 turn "…" and "·" into mojibake.
        ctype = super().guess_type(path)
        if ctype.split(";")[0] in _UTF8_TYPES and "charset" not in ctype:
            ctype += "; charset=utf-8"
        return ctype

    def end_headers(self):
        # Static files (pages, scripts, styles) carry only Last-Modified, so browsers reused old copies for hours and could run
        # a new script against an old module. "no-cache" makes them ask each time; unchanged files answer 304.
        if not any(h.lower().startswith(b"cache-control") for h in getattr(self, "_headers_buffer", [])):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _read_body(self) -> bytes | None:
        raw = int(self.headers.get("Content-Length", 0))
        if raw > MAX_BODY:
            self._json({"error": "Request body too large"}, 413)
            return None
        return self.rfile.read(raw) if raw else None

    # ── Camera proxy ──────────────────────────────────────────────────────────

    @staticmethod
    def _open_camera(camera_url: str):
        parsed = urllib.parse.urlparse(camera_url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=10)
        conn.request("GET", path)
        return conn, conn.getresponse()

    @staticmethod
    def _request_camera_enable(p, wait_s: float = 5) -> bool:
        """Ask the printer to restart its video feed; waits up to wait_s when wait_s > 0."""
        if state.loop is None:
            return False
        fut = asyncio.run_coroutine_threadsafe(p.enable_camera(), state.loop)
        if wait_s <= 0:
            return True
        try:
            return bool(fut.result(timeout=wait_s))
        except Exception:
            return False

    def _proxy_camera(self):
        printer_id = urllib.parse.unquote(self.path[len("/api/camera/"):].split("?")[0])
        p = state.printers.get(printer_id)
        if not p or not p.camera_url:
            self._json({"error": "Camera not available"}, 404)
            return
        conn = None
        upstream_lost = False
        try:
            conn, upstream = self._open_camera(p.camera_url)
            if upstream.status != 200 and self._request_camera_enable(p):
                # The feed was switched off (CC1 does this at print start); retry once it is re-enabled.
                conn.close()
                time.sleep(CAMERA_REENABLE_SETTLE_S)
                conn, upstream = self._open_camera(p.camera_url)
            if upstream.status != 200:
                self._json({"error": f"camera returned {upstream.status}"}, 503)
                return
            try:
                # A feed that goes silent ends the response, so the browser can reconnect
                # instead of showing its last frame indefinitely.
                conn.sock.settimeout(CAMERA_STALL_TIMEOUT_S)
            except Exception:
                pass
            ct = upstream.getheader("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                try:
                    chunk = upstream.read(8192)
                except OSError:
                    upstream_lost = True
                    break
                if not chunk:
                    upstream_lost = True
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            pass
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        if upstream_lost:
            # Re-enable now so the browser's reconnect finds the feed running.
            self._request_camera_enable(p, wait_s=0)

    # ── Thumbnail proxy ───────────────────────────────────────────────────────
    # CC1: http://{ip}:80/thumbnail/{bare_filename}
    # CC2: no accessible thumbnail endpoint — skipped client-side

    def _proxy_thumbnail(self):
        rest = self.path[len("/api/thumbnail/"):]
        parts = rest.split("/", 1)
        if len(parts) != 2:
            self._json({"error": "Bad request"}, 400)
            return
        printer_id = urllib.parse.unquote(parts[0])
        bare_name  = urllib.parse.unquote(parts[1])
        p = state.printers.get(printer_id)
        if not p:
            self._json({"error": "Printer not found"}, 404)
            return

        token = p.access_code or "123456"

        # CC2 has no accessible thumbnail endpoint — caller should not request one
        if p.printer_type == "cc2":
            self._json({"error": "Thumbnail unavailable"}, 404)
            return

        # CC1: dedicated /thumbnail/{bare_filename} endpoint
        conn = None
        try:
            path = f"/thumbnail/{urllib.parse.quote(bare_name)}"
            conn = http.client.HTTPConnection(p.ip, 80, timeout=5)
            conn.request("GET", path, headers={})
            resp = conn.getresponse()
            if state.DEBUG:
                print(f"[thumb] {bare_name!r} → {resp.status}")
            if resp.status == 200:
                data = resp.read(2 * 1024 * 1024)
                ct   = resp.getheader("Content-Type", "image/png")
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=300")
                self.end_headers()
                self.wfile.write(data)
                return
        except Exception as e:
            if state.DEBUG:
                print(f"[thumb] exception for {bare_name!r}: {e}")
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        self._json({"error": "Thumbnail unavailable"}, 404)

    # ── Spoolman proxy ────────────────────────────────────────────────────────

    def _proxy_spoolman(self, method: str, path: str, body: bytes | None):
        if not path.startswith("/api/v1/"):
            self._json({"error": "Invalid Spoolman path"}, 400)
            return
        try:
            req = urllib.request.Request(
                f"{get_spoolman_url()}{path}",
                data=body,
                headers={"Content-Type": "application/json"} if body else {},
                method=method,
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data   = resp.read()
                status = resp.status
            if not data:
                self.send_response(status)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
            else:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self._json({"error": str(e)}, e.code)
        except Exception as e:
            self._json({"error": f"Spoolman unreachable: {e}"}, 502)

    def _proxy_spoolman_ui(self, method: str, sm_path: str, body: bytes | None = None):
        """Proxy Spoolman's own web UI through our server.

        Rewrites absolute asset paths in HTML responses so they stay within our
        /spoolman/ prefix (required because Spoolman uses paths like /assets/...).
        """
        try:
            headers = {"Content-Type": "application/json"} if body else {}
            req = urllib.request.Request(
                f"{get_spoolman_url()}{sm_path}",
                data=body,
                headers=headers,
                method=method,
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
                status = resp.status
                ct = resp.headers.get("Content-Type", "application/octet-stream")

            # Rewrite absolute paths in HTML so assets load through our proxy
            if "text/html" in ct:
                html = data.decode("utf-8", errors="replace")
                html = html.replace('src="/', 'src="/spoolman/')
                html = html.replace("src='/", "src='/spoolman/")
                html = html.replace('href="/', 'href="/spoolman/')
                html = html.replace("href='/", "href='/spoolman/")
                # Injected before Spoolman's own scripts to fix proxied-subpath issues:
                # 1. Strip /spoolman from URL on load so React Router sees / and matches.
                # 2. Click-intercept on href="/" forces reload via /spoolman/ (Home fix).
                # 3. Patch fetch/XHR so API/asset requests route via /spoolman/.
                # 4. MutationObserver fixes dynamically-rendered <img> src attrs.
                # 5. Unregister SW + clear its caches (wrong scope, bypasses our patches).
                # Note: Spoolman's WebSocket live-push connections are not proxied —
                # they will fail silently; all page data still loads via HTTP polling.
                _patch = (
                    "<script>(function(){"
                    # Strip /spoolman prefix from URL before React Router initialises
                    "if(window.location.pathname.startsWith('/spoolman')){"
                    "history.replaceState(null,'',"
                    "window.location.pathname.slice('/spoolman'.length)||'/');"
                    "}"
                    # Intercept Home link clicks — reload via /spoolman/ so our script
                    # re-runs and React Router sees / correctly.
                    "document.addEventListener('click',function(e){"
                    "var el=e.target&&e.target.closest('a');"
                    "if(el&&(el.getAttribute('href')==='/'||el.pathname==='/')&&"
                    "el.origin===location.origin){"
                    "e.preventDefault();e.stopPropagation();"
                    "window.location.replace('/spoolman/');}},true);"
                    # Rewrite helper for fetch/XHR/img
                    "function _rw(u){return(typeof u==='string'&&u.startsWith('/')&&"
                    "!u.startsWith('/spoolman')&&!u.startsWith('/api/')&&!u.startsWith('/ws'))"
                    "?'/spoolman'+u:u;}"
                    # Patch fetch
                    "var _f=window.fetch;"
                    "window.fetch=function(u,o){return _f.call(this,_rw(u),o);};"
                    # Patch XHR
                    "var _o=XMLHttpRequest.prototype.open;"
                    "XMLHttpRequest.prototype.open=function(m,u){"
                    "arguments[1]=_rw(u);return _o.apply(this,arguments);};"
                    # MutationObserver for dynamically-rendered <img>/<link> elements
                    "var _mo=new MutationObserver(function(ms){"
                    "ms.forEach(function(m){m.addedNodes.forEach(function(n){"
                    "if(n.nodeType!==1)return;"
                    "var els=[n].concat(Array.from(n.querySelectorAll('img,link')));"
                    "els.forEach(function(el){"
                    "var s=el.getAttribute('src');if(s&&el.tagName==='IMG')el.src=_rw(s);"
                    "var h=el.getAttribute('href');if(h&&el.tagName==='LINK')el.href=_rw(h);"
                    "});});});});"
                    "_mo.observe(document,{childList:true,subtree:true});"
                    # Unregister SW and clear its caches so stale SW doesn't intercept
                    "if('serviceWorker'in navigator){"
                    "navigator.serviceWorker.getRegistrations&&"
                    "navigator.serviceWorker.getRegistrations().then(function(rs){"
                    "rs.forEach(function(r){r.unregister();});});"
                    "'caches'in window&&caches.keys().then(function(ns){"
                    "ns.forEach(function(n){caches.delete(n);});});"
                    "navigator.serviceWorker.register=function(){"
                    "return Promise.resolve({scope:'/'});};"
                    "}"
                    "})();</script>"
                )
                html = html.replace("</head>", _patch + "</head>", 1)
                data = html.encode("utf-8")
                ct = "text/html; charset=utf-8"

            self.send_response(status)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            body_err = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_err)))
            self.end_headers()
            self.wfile.write(body_err)
        except Exception as e:
            self._json({"error": f"Spoolman unreachable: {e}"}, 502)

    # ── HTTP verbs ────────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path == "/cert.pem":
            data = CERT_FILE.read_bytes() if CERT_FILE.exists() else b""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-pem-file")
            self.send_header("Content-Disposition", 'attachment; filename="spooler-cert.pem"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path in ("/manifest.json", "/icon.svg", "/sw.js", "/favicon.ico"):
            super().do_GET()
            return
        if self.path == "/config.js":
            ws_port  = int(os.getenv("WS_PORT",  "8765"))
            wss_port = int(os.getenv("WSS_PORT", "8766"))
            body = (
                f"window.SPOOLER_WS_PORT  = {ws_port};\n"
                f"window.SPOOLER_WSS_PORT = {wss_port};\n"
                f"window.SPOOLER_BUILD    = {json.dumps(ui_build())};\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        # Spoolman — either proxy through our server or redirect directly to it
        if self.path in ("/spoolman", "/spoolman/"):
            if PROXY_SPOOLMAN:
                if self.path == "/spoolman":
                    self.send_response(302)
                    self.send_header("Location", "/spoolman/")
                    self.end_headers()
                else:
                    self._proxy_spoolman_ui("GET", "/")
            else:
                self.send_response(302)
                self.send_header("Location", get_spoolman_url() + "/")
                self.end_headers()
            return
        if PROXY_SPOOLMAN and self.path.startswith("/spoolman/"):
            sm_path = self.path[len("/spoolman"):]
            self._proxy_spoolman_ui("GET", sm_path or "/")
            return
        # Spoolman's own JS calls /api/v1/ directly — only proxy when enabled
        if PROXY_SPOOLMAN and self.path.startswith("/api/v1/"):
            self._proxy_spoolman_ui("GET", self.path)
            return
        if self.path == "/api/health":
            # Liveness probe for supervisor.py — it reveals nothing but up/down.
            ok = health.event_loop_responsive()
            self._json({"status": "ok" if ok else "unresponsive", "uptime_s": health.uptime_s()},
                       200 if ok else 503)
            return

        if self.path == "/api/build":
            self._json({"build": ui_build()})
            return

        if self.path == "/api/push-public-key":
            # Public key is safe to expose — it is literally a public key.
            # Service workers need it to renew expired subscriptions without a session cookie.
            if WEBPUSH_AVAILABLE and get_public_key():
                self._json({"publicKey": get_public_key()})
            else:
                self._json({"error": "Web Push not available"}, 503)
            return

        if self.path == "/api/notification-settings":
            self._json(load_notif_settings())
            return

        if self.path == "/api/assistant/settings":
            self._assistant_settings()
            return

        if self.path.split("?", 1)[0] == "/api/assistant/status":
            self._assistant_status(None)
            return

        if self.path.split("?", 1)[0] == "/api/assistant/run-log":
            self._assistant_run_log()
            return

        if self.path.split("?", 1)[0].startswith("/api/assistant/"):
            self._json({"error": "Not found"}, 404)          # an unknown Assistant path answers JSON, never the HTML page
            return

        if self.path.startswith("/api/camera/"):
            self._proxy_camera()
        elif self.path.startswith("/api/thumbnail/"):
            self._proxy_thumbnail()
        elif self.path == "/api/printers":
            self._json([p.to_dict() for p in state.printers.values()])
        elif self.path == "/api/dashboard":
            self._jobs_call(dashboard.snapshot, broadcast=False)
        elif self.path == "/api/system-status":
            self._json(health.snapshot())
        elif self.path == "/api/jobs":
            self._json({"jobs": jobs.list_jobs()})
        elif self.path == "/api/slicer":
            self._json(slicer.status())
        elif self.path == "/api/slicer/presets":
            self._jobs_call(slicer.stock_presets, broadcast=False)
        elif self.path == "/api/slicer/spools":
            self._jobs_call(farm.list_spools, broadcast=False)
        elif self.path == "/api/defaults":
            self._jobs_call(lambda: {"defaults": remembered_defaults.get()}, broadcast=False)
        elif self.path == "/api/splits":
            self._json({"splits": splits.list_splits()})
        elif self.path == "/api/splits/status":
            self._json(splits.status())
        elif self.path.startswith("/api/splits/"):
            self._handle_splits_get()
        elif self.path.split("?", 1)[0] == "/api/projects":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            include_quick = (query.get("include_quick") or [""])[0] in ("1", "true")
            self._json({"projects": projects.list_projects(include_quick=include_quick)})
        elif self.path.startswith("/api/projects/"):
            self._handle_projects_get()
        elif self.path.startswith("/api/slicer/profiles/") and self.path.split("?", 1)[0].endswith("/settings"):
            self._jobs_call(lambda: slicer.bundle_settings(self.path.split("?", 1)[0].split("/")[4]), broadcast=False)
        elif self.path.startswith("/api/jobs/") and self.path.endswith("/events"):
            self._jobs_call(lambda: {"events": jobs.job_events(self._job_id())}, broadcast=False)
        elif self.path.startswith("/api/jobs/") and self.path.split("?", 1)[0].endswith("/preflight"):
            printer_id = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("printer_id") or [""])[0]
            self._jobs_call(lambda: preflight.check_job_id(self._job_id(), printer_id or None), broadcast=False)
        elif self.path.startswith("/api/jobs/") and self.path.endswith("/toolpath"):
            self._send_toolpath()
        elif self.path.startswith("/api/jobs/") and self.path.split("?", 1)[0].endswith("/snapshot"):
            self._send_snapshot(lambda: jobs.snapshot_file(self._job_id()), "no-store")
        elif self.path.startswith("/api/printers/") and self.path.endswith("/bed-cleared"):
            self._jobs_call(lambda: bed_cleared.status(self._path_printer_id()), broadcast=False)
        elif self.path.startswith("/api/printers/") and self.path.endswith("/hardware"):
            self._jobs_call(lambda: preflight.printer_hardware(self._path_printer_id()), broadcast=False)
        elif self.path == "/api/prints" or self.path.startswith("/api/prints?"):
            self._jobs_call(lambda: print_history.search(**self._print_filters(paged=True)), broadcast=False)
        elif self.path == "/api/slice-log" or self.path.startswith("/api/slice-log?"):
            project = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("project_id") or [""])[0]
            self._jobs_call(lambda: time_estimate.slice_log(int(project) if project.isdigit() else None), broadcast=False)
        elif self.path == "/api/outcome-prompts":
            self._jobs_call(lambda: {"awaiting": outcome_prompt.awaiting(), "reminder_delay_s": outcome_prompt.REMINDER_DELAY_S},
                            broadcast=False)
        elif self.path.startswith("/api/prints/") and self.path.split("?", 1)[0].endswith("/snapshot"):
            self._send_snapshot(lambda: outcome_prompt.snapshot_file(self._job_id()), "private, max-age=3600")
        elif self.path == "/api/failure-reasons":
            self._json({"failure_reasons": list(print_history.FAILURE_REASONS),
                        "cancel_reasons": list(print_history.CANCEL_REASONS),
                        "labels": print_history.FAILURE_REASON_LABELS})
        elif self.path.startswith("/api/prints/export.csv"):
            self._send_prints_csv()
        elif self.path.startswith("/api/prints/"):
            self._jobs_call(lambda: print_history.get_print(self._job_id()), broadcast=False)
        elif self.path.startswith("/api/lookup-ean"):
            qs     = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            ean    = params.get("ean", [""])[0].strip()
            if not ean:
                self._json({"error": "ean required"}, 400)
                return
            db = get_spoolman_db()
            for item in db:
                if ean in (item.get("ean") or []):
                    self._json(item)
                    return
            self._json({"error": "Not found"}, 404)
        elif self.path == "/api/filament-meta":
            db     = get_spoolman_db()
            brands = sorted({item.get("manufacturer", "") for item in db if item.get("manufacturer")})
            mat_map: dict = {}
            for item in db:
                mat = item.get("material") or ""
                den = item.get("density")
                if mat and mat not in mat_map and den:
                    mat_map[mat] = den
            materials = [{"name": m, "density": mat_map[m]} for m in sorted(mat_map)]
            self._json({"brands": brands, "materials": materials})
        elif self.path.startswith("/api/spoolman"):
            self._proxy_spoolman("GET", self.path[len("/api/spoolman"):], None)
        elif (
            PROXY_SPOOLMAN
            and "text/html" in self.headers.get("Accept", "")
            and self.path.split("?")[0] not in ("/", "")
            and not Path(self.path.split("?")[0]).suffix
        ):
            # Spoolman SPA sub-route (e.g. /spools after client-side navigation + refresh)
            self._proxy_spoolman_ui("GET", "/")
        else:
            # Root/directory paths fall through to index.html via super().
            # Static assets not in public/ are proxied to Spoolman when enabled
            # (handles favicon, kofi logo, etc. rendered without /spoolman/ prefix).
            path_part = self.path.lstrip("/").split("?")[0] or "index.html"
            local = Path(__file__).parent / "public" / path_part
            if local.is_file():
                super().do_GET()
            elif PROXY_SPOOLMAN and Path(path_part).suffix:
                self._proxy_spoolman_ui("GET", self.path.split("?")[0])
            else:
                super().do_GET()

    def do_PATCH(self):
        if self.path.startswith("/api/jobs/"):
            self._jobs_call(lambda: jobs.update_job(self._job_id(), self._json_body()))
            return
        if self.path == "/api/defaults":
            self._jobs_call(lambda: {"defaults": remembered_defaults.save(self._json_body())}, broadcast=False)
            return
        if self.path.startswith("/api/prints/"):
            self._jobs_call(lambda: print_history.update_notes(self._job_id(), self._json_body().get("notes")),
                            broadcast=False)
            return
        if self.path.startswith("/api/projects/") and len(self.path.split("?", 1)[0].split("/")) == 4:
            self._jobs_call(lambda: projects.update_project(self._job_id(), self._json_body()), broadcast=False)
            return
        if PROXY_SPOOLMAN and self.path.startswith("/api/v1/"):
            self._proxy_spoolman_ui("PATCH", self.path, self._read_body())
            return
        if self.path.startswith("/api/spoolman"):
            self._proxy_spoolman("PATCH", self.path[len("/api/spoolman"):], self._read_body())
        else:
            self._json({"error": "Not found"}, 404)

    def do_DELETE(self):
        if self.path.startswith("/api/jobs/") and "/holds/" in self.path:
            parts = self.path.split("?", 1)[0].split("/")  # ['', 'api', 'jobs', <id>, 'holds', <hold id>]
            self._jobs_call(lambda: holds.remove(self._job_id(), self._int(parts[5], "Hold not found")))
            return
        if self.path.startswith("/api/jobs/"):
            self._jobs_call(lambda: jobs.delete_job(self._job_id()))
            return
        if self.path.startswith("/api/slicer/presets/"):
            parts = self.path.split("?", 1)[0].split("/")  # ['', 'api', 'slicer', 'presets', <kind>, <name>]
            if len(parts) == 6:
                self._jobs_call(lambda: slicer.delete_user_preset(parts[4], urllib.parse.unquote(parts[5])), broadcast=False)
            else:
                self._json({"error": "Not found"}, 404)
            return
        if self.path.startswith("/api/splits/"):
            self._splits_call(lambda: splits.delete_split(self._job_id()))
            return
        if self.path.startswith("/api/projects/"):
            parts = self.path.split("?", 1)[0].split("/")  # ['', 'api', 'projects', <id>, 'models', <model id>]
            if len(parts) == 5 and parts[3] == "import-review":      # cancel a pending import review
                self._jobs_call(lambda: projects.cancel_import(parts[4]), broadcast=False)
            elif len(parts) == 6 and parts[4] == "models" and parts[5].isdigit():
                def remove():
                    self._guard_draft(self._job_id())
                    return projects.delete_model(self._job_id(), int(parts[5]))
                self._jobs_call(remove, broadcast=False)
            elif len(parts) == 4:
                self._jobs_call(lambda: projects.delete_project(self._job_id()), broadcast=False)
            else:
                self._json({"error": "Not found"}, 404)
            return
        if PROXY_SPOOLMAN and self.path.startswith("/api/v1/"):
            self._proxy_spoolman_ui("DELETE", self.path)
            return
        if self.path.startswith("/api/spoolman"):
            self._proxy_spoolman("DELETE", self.path[len("/api/spoolman"):], None)
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path == "/api/push-subscribe":
            self._handle_push_subscribe()
        elif self.path == "/api/push-unsubscribe":
            self._handle_push_unsubscribe()
        elif self.path == "/api/notification-settings":
            self._handle_notif_settings()
        elif self.path == "/api/assistant/settings":
            self._assistant_settings()
        elif self.path == "/api/assistant/run-log/clear":
            self._assistant_run_log()
        elif self.path == "/api/push-test":
            self._handle_push_test()
        elif self.path == "/api/import-filaments":
            self._handle_import_filaments()
        elif self.path == "/api/backup":
            self._handle_backup()
        elif self.path == "/api/jobs" or self.path.startswith("/api/jobs/"):
            self._handle_jobs_post()
        elif self.path.startswith("/api/prints/"):
            self._handle_prints_post()
        elif self.path in ("/api/slicer/profiles", "/api/slicer/presets", "/api/slicer/presets/import", "/api/slicer/spool-match"):
            self._handle_slicer_post()
        elif self.path.startswith("/api/splits/"):
            self._handle_splits_post()
        elif self.path == "/api/projects" or self.path.startswith("/api/projects/"):
            self._handle_projects_post()
        elif self.path.startswith("/api/printers/") and self.path.endswith("/bed-cleared"):
            def confirm():
                body = self._json_body()
                return bed_cleared.confirm(self._path_printer_id(), body.get("print_id"), body.get("outcome"),
                                           body.get("failure_reasons"), body.get("notes"), bool(body.get("reprint")))
            self._jobs_call(confirm, coroutine=True)
        elif self.path.startswith("/api/printers/") and self.path.endswith("/auto-start"):
            self._jobs_call(lambda: bed_cleared.set_auto_start(
                self._path_printer_id(), self._json_body().get("enabled")), broadcast=False)
        elif self.path.startswith("/api/printers/") and self.path.endswith("/hardware"):
            def set_hardware():
                body = self._json_body()
                return preflight.set_printer_hardware(
                    self._path_printer_id(), body.get("nozzle_diameter"), body.get("nozzle_material"))
            self._jobs_call(set_hardware, broadcast=False)
        elif self.path.startswith("/api/v1/"):
            self._proxy_spoolman_ui("POST", self.path, self._read_body())
        elif self.path.startswith("/api/spoolman"):
            self._proxy_spoolman("POST", self.path[len("/api/spoolman"):], self._read_body())
        elif self.path.startswith("/api/upload/"):
            self._handle_upload()
        else:
            self._json({"error": "Not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── Complex POST handlers ─────────────────────────────────────────────────

    def _handle_push_subscribe(self):
        try:
            sub = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        endpoint = sub.get("endpoint", "")
        if not endpoint:
            self._json({"error": "Missing endpoint"}, 400)
            return
        add_subscription(sub)
        self._json({"ok": True})

    def _handle_push_unsubscribe(self):
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        remove_subscription(body.get("endpoint", ""))
        self._json({"ok": True})

    def _assistant_settings(self):
        """GET/POST /api/assistant/settings: CLI path and Jev address (no API key). Contained: any failure is a 4xx/5xx body."""
        try:
            import assistant
            if self.command == "GET":
                self._json(assistant.settings_view())
                return
            try:
                body = json.loads(self._read_body() or b"{}")
                self._json(assistant.save_settings(body))
            except (ValueError, assistant.AssistantError) as e:
                self._json({"error": str(e) or "Bad request"}, 400)
        except Exception as e:
            self._json({"error": f"Assistant settings are unavailable: {e}"}, 500)

    def _assistant_run_log(self):
        """GET /api/assistant/run-log (newest first) and POST /api/assistant/run-log/clear. Contained: any failure is a JSON 500."""
        try:
            import assistant_runlog
            if self.command == "POST":
                assistant_runlog.clear()
            self._json({"entries": assistant_runlog.entries(), "cap": assistant_runlog.CAP})
        except Exception as e:
            self._json({"error": f"The Assistant run log is unavailable: {e}"}, 500)

    def _assistant_status(self, project_id):
        """GET /api/projects/<id>/assistant/status[?fresh=1]: available, or why not. Never fails the page."""
        record = projects.get_project(project_id) if project_id is not None else None     # 404 for an unknown project
        fresh = "fresh=1" in urllib.parse.urlparse(self.path).query.split("&")
        try:
            import assistant
            import assistant_runner
            self._json({**assistant.project_status(record, fresh), "limits": assistant_runner.limits()} if record else assistant.status(fresh))
        except Exception as e:
            self._json({"available": False, "reason": f"The assistant could not start: {e}", "purpose": None})

    def _assistant_call(self, fn):
        """Run an Assistant run operation. The runner is imported here, so a broken Assistant never touches core routes; its
        refusals are JobErrors (400/404/409/503) and anything else is a 500 with a message."""
        try:
            import assistant_runner
        except Exception as e:
            self._json({"error": f"The assistant could not start: {e}"}, 500)
            return
        self._jobs_call(lambda: fn(assistant_runner), broadcast=False)

    def _assistant_proposal_call(self, fn):
        """Review, accept or dismiss a staged Proposal (the module is imported here, like the runner: a broken Assistant never touches core routes)."""
        try:
            import assistant_proposals
        except Exception as e:
            self._json({"error": f"The assistant could not start: {e}"}, 500)
            return
        self._jobs_call(lambda: fn(assistant_proposals), broadcast=False)

    def _handle_notif_settings(self):
        try:
            s = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        save_notif_settings(s)
        self._json({"ok": True})

    def _handle_push_test(self):
        self._read_body()
        if not WEBPUSH_AVAILABLE:
            self._json({"error": "Web Push not available on server"}, 503)
            return
        if not has_subscriptions():
            self._json({"error": "No push subscriptions registered"}, 400)
            return
        send_push_all("Spooler — Test notification", "Push notifications are working!")
        self._json({"ok": True})

    # ── Print-job queue ───────────────────────────────────────────────────────

    def _job_id(self) -> int:
        try:
            return int(self.path.split("?", 1)[0].split("/")[3])
        except (IndexError, ValueError):
            raise jobs.JobError("Job not found", 404)

    @staticmethod
    def _int(value: str, message: str) -> int:
        try:
            return int(value)
        except ValueError:
            raise jobs.JobError(message, 404)

    def _send_snapshot(self, find, cache: str) -> None:
        """Serve a stored JPEG: `find()` returns its path or raises a JobError."""
        try:
            data = find().read_bytes()
        except jobs.JobError as e:
            self._json({"error": str(e)}, e.status)
            return
        except OSError:
            self._json({"error": "No snapshot found"}, 404)
            return
        self._send_bytes(data, "image/jpeg", cache=cache)

    def _json_body(self) -> dict:
        try:
            body = json.loads(self._read_body() or b"{}")
        except ValueError:
            raise jobs.JobError("Request body must be JSON")
        if not isinstance(body, dict):
            raise jobs.JobError("Request body must be a JSON object")
        return body

    def _jobs_call(self, fn, *, coroutine: bool = False, broadcast: bool = True) -> None:
        """Run a jobs operation and reply with its result, or with the JobError it raised.

        Coroutines (starting and finishing jobs) run on the main event loop and
        broadcast their own updates; plain calls are broadcast from here.
        """
        try:
            if coroutine:
                if state.loop is None:
                    raise jobs.JobError("Spooler is still starting", 503)
                # Starting a job analyses its G-code first, which takes a while for very large files
                result = asyncio.run_coroutine_threadsafe(fn(), state.loop).result(timeout=180)
            else:
                result = fn()
        except jobs.JobError as e:
            self._json({"error": str(e), **getattr(e, "extra", {}), **({"issues": e.issues} if getattr(e, "issues", None) else {})}, e.status)
            return
        if broadcast and not coroutine:
            jobs.broadcast_threadsafe()
        self._json(result if result is not None else {"ok": True})

    def _path_printer_id(self) -> str:
        parts = self.path.split("?", 1)[0].split("/")  # ['', 'api', 'printers', <id>, 'hardware']
        if len(parts) < 4 or not parts[3]:
            raise jobs.JobError("Printer not found", 404)
        return urllib.parse.unquote(parts[3])

    def _send_toolpath(self):
        try:
            data = preflight.toolpath_file(self._job_id()).read_bytes()
        except jobs.JobError as e:
            self._json({"error": str(e)}, e.status)
            return
        except OSError:
            self._json({"error": "Preview not available"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle_jobs_post(self):
        path = self.path.split("?", 1)[0]
        parts = path.split("/")  # ['', 'api', 'jobs', <id>, <action>]
        action = parts[4] if len(parts) == 5 else None
        if path == "/api/jobs/upload":
            self._handle_job_upload()
        elif path == "/api/jobs":
            self._jobs_call(self._create_printer_file_job)
        elif path == "/api/jobs/reorder":
            self._jobs_call(lambda: jobs.reorder(self._json_body().get("order")))
        elif path == "/api/jobs/assign":
            def assign():
                body = self._json_body()
                return {"jobs": jobs.assign_printers(body.get("job_ids"), body.get("printer_ids"))}
            self._jobs_call(assign)
        elif action == "start":
            def start():
                body = self._json_body()
                return jobs.start_job(self._job_id(), body.get("printer_id"),
                                      acknowledge_warnings=bool(body.get("acknowledge_warnings")))
            self._jobs_call(start, coroutine=True)
        elif action == "finish":
            self._jobs_call(lambda: jobs.finish_job(self._job_id(), self._json_body().get("outcome")),
                            coroutine=True)
        elif action == "requeue":
            self._jobs_call(lambda: jobs.requeue(self._job_id()))
        elif action == "holds":
            self._jobs_call(lambda: holds.add(self._job_id(), self._json_body()))
        elif action == "acknowledge-warnings":
            self._jobs_call(lambda: jobs.acknowledge_warnings(self._job_id(), self._json_body().get("printer_id")),
                            broadcast=False)
        else:
            self._json({"error": "Not found"}, 404)

    def _handle_job_upload(self):
        # The body is the raw G-code file, streamed to disk rather than held in memory.
        self.close_connection = True  # an early rejection leaves the body unread
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        self._jobs_call(lambda: jobs.create_upload_job(
            self.rfile, length, query.get("filename", [""])[0], query.get("printer_id", [""])[0] or None))

    def _create_printer_file_job(self) -> dict:
        body = self._json_body()
        return jobs.create_printer_file_job(body.get("printer_id"), body.get("printer_file"), body)

    # ── Print history ─────────────────────────────────────────────────────────

    # ── Slicing ───────────────────────────────────────────────────────────────

    def _handle_slicer_post(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/slicer/profiles":
            self._jobs_call(self._create_profile, broadcast=False)
        elif path == "/api/slicer/spool-match":
            def match():
                body = self._json_body()
                return farm.spool_match(body.get("spool_id"), str(body.get("bundle_id") or ""), str(body.get("bed") or "textured"))
            self._jobs_call(match, broadcast=False)
        elif path == "/api/slicer/presets":
            self._jobs_call(self._save_preset, broadcast=False)
        elif path == "/api/slicer/presets/import":
            self._jobs_call(lambda: slicer.import_user_preset(self._json_body().get("preset"), self._json_body().get("name")),
                            broadcast=False)
        else:
            self._json({"error": "Not found"}, 404)

    def _save_preset(self) -> dict:
        body = self._json_body()
        kind = str(body.get("kind") or "")
        record = slicer.save_user_preset(kind, str(body.get("name") or ""), str(body.get("base") or ""), body.get("changes"))
        return {"preset": record["name"], "type": kind}

    def _create_profile(self) -> dict:
        body = self._json_body()
        if body.get("from_bundle"):  # a variant of a locked profile, e.g. with tree supports
            return slicer.derive_bundle(str(body["from_bundle"]), body.get("overrides"), body.get("label"))
        return slicer.create_bundle(str(body.get("machine") or ""), str(body.get("process") or ""),
                                    str(body.get("filament") or ""), body.get("label"), body.get("overrides"))

    # ── Splitting large models ────────────────────────────────────────────────

    def _splits_call(self, fn) -> None:
        self._jobs_call(fn, broadcast=False)
        splits.broadcast_threadsafe()

    def _handle_splits_get(self):
        parts = self.path.split("?", 1)[0].split("/")  # ['', 'api', 'splits', <id>, <kind>, <file>]
        try:
            split_id = self._job_id()
            if len(parts) == 4:
                self._json(splits.get_split(split_id, detail=True))
            elif len(parts) == 5 and parts[4] in ("preview", "parts-preview"):
                self._send_file(splits.file_path(split_id, parts[4]), "application/octet-stream")
            elif len(parts) == 5 and parts[4] == "assembly-plan":
                self._json(splits.assembly_plan(split_id))
            elif len(parts) == 5 and parts[4] == "plan-edits":
                query = urllib.parse.parse_qs(self.path.partition("?")[2])
                if query.get("receivers_for"):
                    self._json(splits.edit_receivers(split_id, query["receivers_for"][0].split(",")))
                else:
                    self._json(splits.assembly_edits(split_id))
            elif len(parts) == 5 and parts[4] == "guide":
                self._json(splits.guide_status(split_id))
            elif len(parts) == 5 and parts[4] == "guide.pdf":
                path, name = splits.guide_path(split_id)
                self._send_file(path, "application/pdf", download_name=name)
            elif len(parts) == 6 and parts[4] == "parts" and parts[5].endswith(".stl") and parts[5][:-4].isdigit():
                path, name = splits.part_path(split_id, int(parts[5][:-4]))
                self._send_file(path, "model/stl", download_name=name)
            else:
                self._json({"error": "Not found"}, 404)
        except jobs.JobError as e:
            self._json({"error": str(e)}, e.status)

    def _send_bytes(self, body: bytes, content_type: str, download_name: str | None = None, cache: str = "no-store") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str, download_name: str | None = None) -> None:
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        with path.open("rb") as fh:
            while chunk := fh.read(1024 * 1024):
                self.wfile.write(chunk)

    def _handle_splits_post(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/splits/upload":
            self._handle_split_upload()
            return
        parts = path.split("/")  # ['', 'api', 'splits', <id>, <action>]
        actions = {
            "settings": lambda: splits.update_settings(self._job_id(), self._json_body()),
            "plan":     lambda: splits.request_plan(self._job_id(), self._json_body()),
            "cuts":     lambda: splits.save_cuts(self._job_id(), self._json_body()),
            "split":    lambda: splits.request_split(self._job_id()),
            "retry":    lambda: splits.retry(self._job_id()),
            "queue":    lambda: splits.queue_parts(self._job_id(), self._json_body()),
            "guide":    lambda: splits.start_guide(self._job_id(), self._json_body()),
            "plan-edits": lambda: splits.edit_plan(self._job_id(), self._json_body()),
        }
        action = parts[4] if len(parts) == 5 else None
        if action not in actions:
            self._json({"error": "Not found"}, 404)
            return
        self._splits_call(actions[action])

    # ── Slicer workspace projects ─────────────────────────────────────────────

    def _handle_projects_get(self):
        parts = self.path.split("?", 1)[0].split("/")  # ['', 'api', 'projects', <id>, <section>, <item id>, <what>]
        if len(parts) == 5 and parts[3] == "import-review":      # a pending import review, by token
            self._jobs_call(lambda: projects.get_import_review(parts[4]), broadcast=False)
            return
        try:
            project_id = self._job_id()
            if len(parts) == 4:
                self._json(projects.get_project(project_id))
            elif len(parts) == 5 and parts[4] == "imports":
                self._json({"imports": projects.import_records(project_id)})
            elif len(parts) == 7 and parts[4] == "imports" and parts[6] == "source":
                path, name = projects.import_source(project_id, parts[5])
                self._send_file(path, "model/3mf", download_name=safe_filename(name, "import.3mf", ""))
            elif len(parts) == 6 and parts[4] == "assistant" and parts[5] == "status":
                self._assistant_status(project_id)
            elif len(parts) == 7 and parts[4] == "assistant" and parts[5] == "runs":
                self._assistant_call(lambda runner: runner.get(project_id, parts[6]))
            elif len(parts) == 6 and parts[4] == "assistant" and parts[5] == "proposals":
                self._assistant_proposal_call(lambda proposals: proposals.pending(project_id))
            elif len(parts) == 7 and parts[4] == "assistant" and parts[5] == "proposals":
                self._assistant_proposal_call(lambda proposals: proposals.review(project_id, parts[6]))
            elif len(parts) == 5 and parts[4] == "jobs":
                self._json({"jobs": projects.project_jobs(project_id)})
            elif len(parts) == 5 and parts[4] == "status":
                self._json(projects.plate_status(project_id))
            elif len(parts) == 5 and parts[4] == "guides":
                self._json({"guides": projects.guides(project_id)})
            elif len(parts) == 5 and parts[4] == "intake":
                self._json(projects.intake(project_id))
            elif len(parts) == 5 and parts[4] == "intake-files":
                self._guard_draft(project_id)
                self._json(projects.intake_files(project_id))
            elif len(parts) == 5 and parts[4] == "quick-options":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                self._json(projects.quick_options(project_id, (query.get("preset") or [None])[0]))
            elif len(parts) == 5 and parts[4] == "export-plan":
                self._guard_draft(project_id)
                self._json(projects.export_plan(project_id))
            elif len(parts) == 5 and parts[4] == "export.3mf":
                self._guard_draft(project_id)
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if query.get("revision") or query.get("fingerprint"):      # the reviewed plan: refused if the Project changed since
                    artifact = projects.export_project(project_id, (query.get("revision") or [None])[0], (query.get("fingerprint") or [None])[0])
                else:                                                      # the direct action: the same plan, made now
                    plan = projects.export_plan(project_id)
                    artifact = projects.export_project(project_id, plan["revision"], plan["fingerprint"])
                self._send_bytes(artifact["body"], artifact["content_type"], download_name=artifact["filename"])
            elif len(parts) == 5 and parts[4] == "export-package.zip":
                self._guard_draft(project_id)
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)       # only for a plan the caller reviewed
                artifact = projects.export_project(project_id, (query.get("revision") or [None])[0], (query.get("fingerprint") or [None])[0], export_plan.PACKAGE)
                self._send_bytes(artifact["body"], artifact["content_type"], download_name=artifact["filename"])
            elif len(parts) == 7 and parts[4] == "models" and parts[6] == "preview":
                self._send_file(projects.model_preview(project_id, int(parts[5])), "application/octet-stream")
            elif len(parts) == 7 and parts[4] == "objects" and parts[6] == "orientation":
                query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
                if query.get("face"):
                    query["face"] = [float(x) for x in query["face"].split(",")]   # "x,y,z", a direction in the model's space
                self._json(projects.suggest_orientation(project_id, int(parts[5]), query))
            elif len(parts) == 7 and parts[4] == "plates" and parts[6] == "effective-settings":
                self._guard_draft(project_id)
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                self._json(projects.effective_settings(
                    project_id, int(parts[5]), (query.get("revision") or [None])[0],
                    object_id=int(query["object"][0]) if query.get("object") else None,
                    search=(query.get("search") or [""])[0], changed_only=(query.get("changed") or ["0"])[0] in ("1", "true")))
            elif len(parts) == 7 and parts[4] == "plates" and parts[6] == "slice-inputs":
                self._guard_draft(project_id)
                self._json(projects.slice_inputs(project_id, int(parts[5])))
            elif len(parts) == 7 and parts[4] == "plates" and parts[6] == "fit":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                self._json(farm.plate_fit(projects.get_project(project_id), int(parts[5]), (query.get("printer_id") or [None])[0]))
            elif len(parts) == 7 and parts[4] == "plates" and parts[6] == "preview.png":
                self._send_bytes(projects.plate_preview(project_id, int(parts[5])), "image/png", cache="private, max-age=86400")
            elif len(parts) == 7 and parts[4] == "plates" and parts[6] == "analysis":
                self._json(projects.plate_analysis(project_id, int(parts[5])))
            elif len(parts) == 7 and parts[4] == "plates" and parts[6] in ("toolpath", "gcode"):
                plate_id = int(parts[5])
                path = projects.plate_file(project_id, plate_id, parts[6])
                if parts[6] == "gcode":
                    record = projects.get_project(project_id)
                    plate = next((p for p in record["data"]["plates"] if p["id"] == plate_id), {"name": f"Plate {plate_id}"})
                    name = safe_filename(f"{record['name']}_{plate['name']}") + ".gcode"
                    self._send_file(path, "text/plain; charset=utf-8", download_name=name)
                else:
                    self._send_file(path, "application/octet-stream")
            else:
                self._json({"error": "Not found"}, 404)
        except ValueError:
            self._json({"error": "Not found"}, 404)
        except jobs.JobError as e:
            self._json({"error": str(e), **({"issues": e.issues} if getattr(e, "issues", None) else {})}, e.status)

    def _handle_projects_post(self):
        path = self.path.split("?", 1)[0]
        parts = path.split("/")  # ['', 'api', 'projects', <id>, <section>, <item id>, <action>]
        if path == "/api/projects":
            def create():
                body = self._json_body()
                try:
                    split_id = int(body["split_id"]) if body.get("split_id") not in (None, "") else None
                except (TypeError, ValueError):
                    raise jobs.JobError("split_id must be a split number")
                return projects.create_project(body.get("name", ""), split_id=split_id, draft=bool(body.get("draft")))
            self._jobs_call(create, broadcast=False)
            return
        if path == "/api/projects/upload":
            self._handle_project_upload(new_project=True)
            return
        if path == "/api/projects/import-review":              # stage and inspect a 3MF; nothing changes yet
            self._handle_import_review_upload()
            return
        if len(parts) == 6 and parts[3] == "import-review" and parts[5] == "resolve":
            def resolve():
                body = self._json_body()
                pending = projects.get_import_review(parts[4])["binding"]["target"]
                if pending:
                    self._guard_draft(pending["project_id"], body)
                return projects.resolve_import(parts[4], {k: v for k, v in body.items() if k != "draft_token"})
            self._splits_call(resolve)
            return
        if len(parts) == 6 and parts[3] == "import-review" and parts[5] == "confirm":
            def confirm():
                body = self._json_body()
                pending = projects.get_import_review(parts[4])["binding"]["target"]
                if pending:
                    self._guard_draft(pending["project_id"], body)
                return projects.confirm_import(parts[4], body)
            self._splits_call(confirm)
            return
        if len(parts) == 5 and parts[3].isdigit() and parts[4] == "create":   # promote a draft
            def create_it():
                body = self._json_body()
                self._guard_draft(self._job_id(), body)
                return projects.create_draft(self._job_id(), body.get("name", ""), arrange=body.get("arrange") is not False)
            self._jobs_call(create_it, broadcast=False)
            return
        if len(parts) == 5 and parts[3].isdigit() and parts[4] == "discard-draft":   # navigator.sendBeacon on page close
            def discard():
                self._guard_draft(self._job_id())
                projects.discard_draft(self._job_id())
                return {"ok": True}
            self._jobs_call(discard, broadcast=False)
            return
        if len(parts) == 5 and parts[3].isdigit() and parts[4] == "export-lossy":
            # The explicit lossy whole-Project 3MF: only for an acknowledgement of the reviewed report, never from export.3mf
            try:
                body = self._json_body()
                self._guard_draft(self._job_id(), body)
                artifact = projects.export_lossy(self._job_id(), body)
            except jobs.JobError as e:
                self._json({"error": str(e), **({"issues": e.issues} if getattr(e, "issues", None) else {})}, e.status)
                return
            self._send_bytes(artifact["body"], artifact["content_type"], download_name=artifact["filename"])
            return
        if len(parts) == 6 and parts[4] == "settings" and parts[5] in ("review", "adopt"):
            def review_setting_change():   # preview or apply a preset refresh / Spoolman adoption (revision- and token-checked)
                body = self._json_body()
                self._guard_draft(self._job_id(), body)
                return (settings_review.preview if parts[5] == "review" else settings_review.apply)(self._job_id(), body)
            self._jobs_call(review_setting_change, broadcast=False)
            return
        if len(parts) == 6 and parts[4:6] == ["settings", "resolve"]:
            def resolve_setting():
                body = self._json_body()
                self._guard_draft(self._job_id(), body)
                return projects.resolve_setting(self._job_id(), body)
            self._jobs_call(resolve_setting, broadcast=False)
            return
        def split_of(body):   # which of a project's splits the call is about; none = the first (Quick print has one)
            try:
                return int(body["split_id"]) if body.get("split_id") not in (None, "") else None
            except (TypeError, ValueError):
                raise jobs.JobError("split_id must be a split number")
        if len(parts) == 5 and parts[4] in ("auto-place", "fix-scale", "print", "open-as-project", "lay-flat", "rotate"):
            actions = {
                "auto-place": lambda: projects.auto_place(self._job_id(), split_of(self._json_body())),
                "fix-scale":  lambda: projects.fix_scale(self._job_id(), split_of(self._json_body())),
                "open-as-project": lambda: projects.open_as_project(self._job_id()),
                "lay-flat":   lambda: projects.lay_flat(self._job_id(), split_of(self._json_body())),
                "rotate":     lambda: projects.rotate_quarter(self._job_id(), split_of(self._json_body())),
                "print":      lambda: projects.slice_and_print(self._job_id(), self._json_body()),
            }
            self._jobs_call(actions[parts[4]], broadcast=False)
            return
        if len(parts) == 5 and parts[4] == "plan":
            def plan():
                body = self._json_body()
                self._guard_draft(self._job_id(), body)
                return projects.plan_plates(self._job_id(), bool(body.get("allow_tilt")), body.get("pack"))
            self._jobs_call(plan, broadcast=False)
            return
        if len(parts) == 6 and parts[4] == "plan" and parts[5] == "densities":
            def densities():
                body = self._json_body()
                self._guard_draft(self._job_id(), body)
                return projects.plan_densities(self._job_id(), bool(body.get("allow_tilt")))
            self._jobs_call(densities, broadcast=False)
            return
        if len(parts) == 5 and parts[4] == "pack":
            def pack():
                body = self._json_body()
                self._guard_draft(self._job_id(), body)
                return projects.set_pack(self._job_id(), body)
            self._jobs_call(pack, broadcast=False)
            return
        if len(parts) == 6 and parts[4] == "plan" and parts[5] == "accept":
            def accept():
                body = self._json_body()
                return projects.accept_plan(self._job_id(), body.get("plan") or body.get("revision"), body.get("fingerprint"), bool(body.get("allow_tilt")))
            self._jobs_call(accept, broadcast=False)
            return
        if len(parts) == 5 and parts[4] == "orient-all":
            self._jobs_call(lambda: projects.orient_all(self._job_id(), self._json_body()), broadcast=False)
            return
        if len(parts) == 7 and parts[4] == "objects" and parts[5].isdigit() and parts[6] == "orient":
            self._jobs_call(lambda: projects.orient_object(self._job_id(), int(parts[5]), self._json_body()), broadcast=False)
            return
        if len(parts) == 5 and parts[4] in ("layout", "whole", "arrange", "slice", "queue"):
            actions = {
                "layout":  lambda: (lambda body: projects.lay_out_split(self._job_id(), body.get("parts"), split_of(body)))(self._json_body()),
                "whole":   lambda: projects.place_whole_model(self._job_id(), split_of(self._json_body())),
                "arrange": lambda: (lambda body: projects.arrange(self._job_id(), body.get("plates"), body.get("engine")))(self._json_body()),
                "slice":   lambda: (lambda body: projects.request_slices(self._job_id(), body.get("plates"), bool(body.get("send"))))(self._json_body()),
                "queue":   lambda: projects.queue_plates(self._job_id(), self._json_body()),
            }
            self._jobs_call(actions[parts[4]], broadcast=parts[4] == "queue")
            return
        if len(parts) == 5 and parts[4] == "copies":
            def copies():
                body = self._json_body()
                self._guard_draft(self._job_id(), body)
                return projects.set_copies(self._job_id(), body.get("object"), body.get("copies"), body.get("revision"))
            self._jobs_call(copies, broadcast=False)
            return
        if len(parts) == 7 and parts[4] == "models" and parts[5].isdigit() and parts[6] == "retry":
            def retry():
                self._guard_draft(self._job_id(), self._json_body())
                return projects.retry_model(self._job_id(), int(parts[5]))
            self._jobs_call(retry, broadcast=False)
            return
        if len(parts) == 7 and parts[4] == "models" and parts[5].isdigit() and parts[6] == "make-split":   # Done in the add-files dialog
            self._jobs_call(lambda: projects.make_split(self._job_id(), int(parts[5])), broadcast=False)
            return
        if len(parts) == 7 and parts[4] == "models" and parts[5].isdigit() and parts[6] == "settings":
            def rescale():
                body = self._json_body()
                self._guard_draft(self._job_id(), body)
                return projects.rescale_model(self._job_id(), int(parts[5]), body.get("scale"), body.get("up"))
            self._jobs_call(rescale, broadcast=False)
            return
        if len(parts) == 6 and parts[4] == "models" and parts[5] == "upload":
            self._handle_project_upload()
            return
        if len(parts) == 6 and parts[4] == "models" and parts[5] == "from-split":
            def from_split():
                body = self._json_body()
                try:
                    split_id = int(body.get("split_id"))
                except (TypeError, ValueError):
                    raise jobs.JobError("split_id must be a split number")
                return projects.add_split_parts(self._job_id(), split_id, body.get("parts"))
            self._jobs_call(from_split, broadcast=False)
            return
        if len(parts) == 7 and parts[4] == "plates" and parts[5].isdigit() and parts[6] in ("arrange", "slice", "queue", "merge", "reprint"):
            plate_id, action = int(parts[5]), parts[6]

            def merge():
                try:
                    into = int(self._json_body().get("into"))
                except (TypeError, ValueError):
                    raise jobs.JobError("into must be a plate number")
                return projects.merge_plates(self._job_id(), plate_id, into)
            actions = {
                "arrange": lambda: projects.arrange_plate(self._job_id(), plate_id, self._json_body().get("engine")),
                "slice":   lambda: projects.request_slice(self._job_id(), plate_id, bool(self._json_body().get("send"))),
                "queue":   lambda: projects.queue_plate(self._job_id(), plate_id, self._json_body()),
                "merge":   merge,
                "reprint": lambda: projects.reprint_plate(self._job_id(), plate_id),
            }
            self._jobs_call(actions[action], broadcast=action in ("queue", "reprint"))
            return
        if len(parts) == 6 and parts[4] == "assistant" and parts[5] == "preview":
            def preview(runner):
                body = self._json_body()
                return runner.preview(self._job_id(), body.get("message"), body.get("history"))
            self._assistant_call(preview)
            return
        if len(parts) == 6 and parts[4] == "assistant" and parts[5] == "ask":
            def ask(runner):
                body = self._json_body()
                return runner.ask(self._job_id(), body.get("message"), body.get("history"))
            self._assistant_call(ask)
            return
        if len(parts) == 8 and parts[4] == "assistant" and parts[5] == "runs" and parts[7] == "cancel":
            self._assistant_call(lambda runner: runner.cancel(self._job_id(), parts[6]))
            return
        if len(parts) == 8 and parts[4] == "assistant" and parts[5] == "proposals" and parts[7] in ("accept", "dismiss"):
            token, accepting = parts[6], parts[7] == "accept"
            self._assistant_proposal_call(lambda proposals: proposals.accept(self._job_id(), token, self._json_body()) if accepting
                                          else proposals.dismiss(self._job_id(), token))
            return
        self._json({"error": "Not found"}, 404)

    def _guard_draft(self, project_id: int, body: dict | None = None) -> None:
        """A page working on a new project's draft names it by its created_at (`draft_token`, in the query or the body):
        if the project is no longer that draft, say so (409) instead of touching whatever took its id."""
        token = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("draft_token") or [None])[0]
        if token is None and body:
            token = body.get("draft_token")
        if token is not None:
            projects.require_draft(project_id, token)

    def _handle_project_upload(self, new_project: bool = False):
        # The body is the raw model file, streamed to disk; scale, up axis and plate travel in the query string.
        # new_project: the file starts a new project (stored as a split model, analysed before anything else).
        self.close_connection = True  # an early rejection leaves the body unread
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        def value(key: str) -> str:
            return (query.get(key) or [""])[0]

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if new_project:
            self._splits_call(lambda: projects.create_project_from_upload(self.rfile, length, value("filename"),
                                                                          scale=value("scale") or 1.0, up=value("up") or "z",
                                                                          bed=value("bed") or "textured",
                                                                          quick=value("quick") in ("1", "true")))
            return
        def add():
            self._guard_draft(self._job_id())
            return projects.import_model(self._job_id(), self.rfile, length, value("filename"),
                                                     scale=value("scale") or 1.0, up=value("up") or "z",
                                                     plate_id=value("plate") or None)
        self._jobs_call(add, broadcast=False)

    def _handle_import_review_upload(self):
        # The body is the raw 3MF; the Project it would join (`target`, with its Draft token), scale, up axis and plate travel in the query.
        self.close_connection = True  # an early rejection leaves the body unread
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        def value(key: str) -> str:
            return (query.get(key) or [""])[0]

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0

        def stage():
            target = self._int(value("target"), "target must be a Project number") if value("target") else None
            if target is not None:
                self._guard_draft(target)
            return projects.prepare_import(self.rfile, length, value("filename"), target=target, scale=value("scale") or 1.0,
                                           up=value("up") or "z", plate=value("plate") or None, quick=value("quick") in ("1", "true"))
        self._jobs_call(stage, broadcast=False)

    def _handle_split_upload(self):
        # The body is the raw model file, streamed to disk; scale and up axis travel in the query string.
        self.close_connection = True  # an early rejection leaves the body unread
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        def value(key: str) -> str:
            return (query.get(key) or [""])[0]

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        settings = {key: value(key) for key in ("scale", "up") if value(key)}
        self._splits_call(lambda: splits.create_split(self.rfile, length, value("filename"), settings, notes=value("notes")))

    # ── Print history ─────────────────────────────────────────────────────────

    def _print_filters(self, paged: bool = False) -> dict:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        def text(key: str) -> str:
            return (query.get(key) or [""])[0].strip()

        def number(key: str):
            raw = text(key)
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                raise jobs.JobError(f"{key} must be a number")

        unconfirmed = text("unconfirmed").lower()
        filters = {"q": text("q"), "unconfirmed": (unconfirmed in ("1", "true", "yes")) if unconfirmed else None,
                   "printer_id": text("printer_id") or None, "outcome": text("outcome") or None, "since": number("since"), "until": number("until")}
        if paged:
            filters["limit"] = int(number("limit") or 50)
            filters["offset"] = int(number("offset") or 0)
        return filters

    def _send_prints_csv(self):
        try:
            body = print_history.export_csv(**self._print_filters()).encode("utf-8-sig")  # BOM for Excel
        except jobs.JobError as e:
            self._json({"error": str(e)}, e.status)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="spooler-prints-{time.strftime("%Y%m%d")}.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_prints_post(self):
        parts = self.path.split("?", 1)[0].split("/")  # ['', 'api', 'prints', <id>, <action>]
        if len(parts) == 5 and parts[4] == "repeat":
            self._jobs_call(lambda: print_history.repeat_print(self._job_id()))
        elif len(parts) == 5 and parts[4] == "reprint":
            self._jobs_call(lambda: print_history.reprint(self._job_id()))
        elif len(parts) == 5 and parts[4] == "outcome":
            def write():
                body = self._json_body()
                return print_history.set_outcome(self._job_id(), body.get("outcome"),
                                                 body.get("failure_reasons"), body.get("notes"))
            self._jobs_call(write)
        else:
            self._json({"error": "Not found"}, 404)

    def _handle_backup(self):
        self._read_body()
        try:
            result = backup.run_backup("manual")
        except backup.BackupInProgress:
            self._json({"error": "A backup is already running"}, 409)
            return
        except Exception as e:
            self._json({"error": f"Backup failed: {e}"}, 500)
            return
        health.broadcast_status_threadsafe()
        self._json(result)

    def _handle_import_filaments(self):
        try:
            req_body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        brand = req_body.get("brand", "").strip()
        if not brand:
            self._json({"error": "brand required"}, 400)
            return
        db      = get_spoolman_db()
        entries = [f for f in db if f.get("manufacturer", "").lower() == brand.lower()]
        if not entries:
            self._json({"error": f"No filaments found for '{brand}'"}, 404)
            return
        try:
            vurl = f"{get_spoolman_url()}/api/v1/vendor?name={urllib.parse.quote(brand)}"
            with urllib.request.urlopen(vurl, timeout=5) as r:
                vendors = json.loads(r.read())
            if vendors:
                vendor_id = vendors[0]["id"]
            else:
                vreq = urllib.request.Request(
                    f"{get_spoolman_url()}/api/v1/vendor",
                    data=json.dumps({"name": brand}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(vreq, timeout=5) as r:
                    vendor_id = json.loads(r.read())["id"]
        except Exception as e:
            self._json({"error": f"Vendor error: {e}"}, 500)
            return
        try:
            with urllib.request.urlopen(
                f"{get_spoolman_url()}/api/v1/filament?limit=10000", timeout=5
            ) as r:
                existing = {f["article_number"] for f in json.loads(r.read()) if f.get("article_number")}
        except Exception:
            existing = set()
        created = skipped = 0
        for f in entries:
            article = f.get("id", "")
            if article and article in existing:
                skipped += 1
                continue
            body: dict = {
                "vendor_id": vendor_id,
                "material":  f.get("material", ""),
                "density":   f.get("density")  or 1.24,
                "diameter":  f.get("diameter") or 1.75,
                "weight":    f.get("weight")   or 1000,
            }
            if f.get("name"):          body["name"]                  = f["name"]
            if f.get("spool_weight"):  body["spool_weight"]           = f["spool_weight"]
            if f.get("color_hex"):     body["color_hex"]              = f["color_hex"].lstrip("#")
            if f.get("extruder_temp"): body["settings_extruder_temp"] = f["extruder_temp"]
            if f.get("bed_temp"):      body["settings_bed_temp"]      = f["bed_temp"]
            if article:                body["article_number"]         = article
            try:
                freq = urllib.request.Request(
                    f"{get_spoolman_url()}/api/v1/filament",
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(freq, timeout=5) as r:
                    r.read()
                created += 1
            except Exception:
                skipped += 1
        print(f"[Import] {brand}: {created} created, {skipped} skipped")
        self._json({"brand": brand, "created": created, "skipped": skipped, "total": len(entries)})

    def _handle_upload(self):
        printer_id = urllib.parse.unquote(self.path[len("/api/upload/"):])
        printer    = state.printers.get(printer_id)
        if not printer:
            self._json({"error": "Printer not found"}, 404)
            return
        ct = self.headers.get("Content-Type", "")
        if not ct.startswith("multipart/form-data"):
            self._json({"error": "Expected multipart/form-data"}, 400)
            return
        body = self._read_body()
        try:
            req = urllib.request.Request(
                f"http://{printer.ip}/uploadFile/upload",
                data=body,
                headers={"Content-Type": ct},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = resp.read()
            self._json({"ok": True, "response": result.decode("utf-8", errors="replace")})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # ── Response helper ───────────────────────────────────────────────────────

    def _json(self, data, code: int = 200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server(ThreadingHTTPServer):
    # The default listen backlog is 5. Every request is its own connection (HTTP/1.0), so a page load opens dozens at once,
    # and while slicing or splitting keeps Python busy the accept loop falls behind: Windows refuses the overflow and the
    # browser reports "Failed to fetch".
    request_queue_size = 256


from bind_address import LOOPBACK_HOSTS, bind_host  # noqa: E402  (listeners bind to bind_host())


def bind_warning() -> str | None:
    """Plain warning to log when the listeners are reachable from other machines, else None."""
    host = bind_host()
    if host in LOOPBACK_HOSTS:
        return None
    return (f"[Net] WARNING: BIND_HOST={host} - Spooler has no login, so anyone who can reach "
            f"{host} can control the printers.")


def make_http_server(port: int) -> _Server:
    host = bind_host()
    server = _Server((host, port), SPHandler)
    print(f"[HTTP] Serving on http://{server.server_address[0]}:{server.server_address[1]}")
    return server


def run_http(port: int) -> None:
    make_http_server(port).serve_forever()


def run_https(port: int) -> None:
    if not ensure_ssl_cert():
        return
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    server = _Server((bind_host(), port), SPHandler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    server._is_https = True
    print(f"[HTTPS] Serving on https://{server.server_address[0]}:{port}")
    server.serve_forever()
