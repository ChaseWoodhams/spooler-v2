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
import farm
import health
import jobs
import preflight
import print_history
import projects
import slicer
import splits
import state
import time
from auth import (
    AUTH_ENABLED, BCRYPT_AVAILABLE, SECURE_COOKIES, SESSION_COOKIE,
    _auth_ok, _check_rate_limit, _create_session, _get_pw_hash, _get_username,
    _has_password, _invalidate_session, _parse_sid, _reset_rate_limit, _save_auth,
)
from persistence import DATA_DIR, save_printers
from push import (
    WEBPUSH_AVAILABLE, add_subscription, get_public_key, has_subscriptions,
    load_notif_settings, remove_subscription, save_notif_settings, send_push_all,
)
from spoolman import get_spoolman_db, get_spoolman_url

try:
    import bcrypt as _bcrypt
except ImportError:
    _bcrypt = None  # type: ignore

CAMERA_STALL_TIMEOUT_S  = 15   # close a proxied camera stream after this long without data
CAMERA_REENABLE_SETTLE_S = 1   # CC1 needs a moment after re-enable before /video answers

CERT_FILE = DATA_DIR / "cert.pem"
KEY_FILE  = DATA_DIR / "key.pem"

MAX_BODY = 100 * 1024 * 1024  # 100 MB

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


class SPHandler(SimpleHTTPRequestHandler):
    """Serve static files from ./public and handle /api/ calls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent / "public"), **kwargs)

    def log_message(self, fmt, *args):
        pass  # silence access log

    def end_headers(self):
        # Static files (pages, scripts, styles) carry only Last-Modified, so browsers reused old copies for hours and could run
        # a new script against an old module. "no-cache" makes them ask each time; unchanged files answer 304.
        if not any(h.lower().startswith(b"cache-control") for h in getattr(self, "_headers_buffer", [])):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    # ── Auth helpers ─────────────────────────────────────────────────────────

    def _check_auth(self) -> bool:
        if _auth_ok(self):
            return True
        if self.path.startswith("/api/"):
            self._json({"error": "Unauthorized"}, 401)
        else:
            self.send_response(302)
            self.send_header("Location", "/login")
            self.end_headers()
        return False

    def _session_cookie(self, token: str, clear: bool = False) -> str:
        is_secure = SECURE_COOKIES or getattr(self.server, "_is_https", False)
        if clear:
            value = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        else:
            value = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"
        if is_secure:
            value += "; Secure"
        return value

    def _read_body(self) -> bytes | None:
        raw = int(self.headers.get("Content-Length", 0))
        if raw > MAX_BODY:
            self._json({"error": "Request body too large"}, 413)
            return None
        return self.rfile.read(raw) if raw else None

    def _handle_login(self):
        ip = self.client_address[0]
        if not _check_rate_limit(ip):
            self._json({"error": "Too many login attempts, try again later"}, 429)
            return
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        username = body.get("username", "")
        password = body.get("password", "")

        ok = False
        pw_hash = _get_pw_hash()
        if AUTH_ENABLED and BCRYPT_AVAILABLE and _bcrypt and pw_hash and username == _get_username():
            try:
                ok = _bcrypt.checkpw(password.encode(), pw_hash.encode())
            except Exception:
                pass

        if not ok:
            self._json({"error": "Invalid credentials"}, 401)
            return

        _reset_rate_limit(ip)
        token = _create_session()
        resp = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Set-Cookie", self._session_cookie(token))
        self.end_headers()
        self.wfile.write(resp)

    def _handle_logout(self):
        token = _parse_sid(self.headers.get("Cookie", ""))
        _invalidate_session(token)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.send_header("Set-Cookie", self._session_cookie("", clear=True))
        self.end_headers()
        self.wfile.write(b"{}")

    def _handle_setup(self):
        if _has_password():
            self._json({"error": "Already configured"}, 403)
            return
        if not BCRYPT_AVAILABLE or not _bcrypt:
            self._json({"error": "bcrypt not installed on server"}, 500)
            return
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        username = body.get("username", "").strip()
        password = body.get("password", "")
        if not username or not password:
            self._json({"error": "Username and password are required"}, 400)
            return
        if len(password) < 8:
            self._json({"error": "Password must be at least 8 characters"}, 400)
            return
        try:
            pw_hash = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
            _save_auth(username, pw_hash)
        except Exception as e:
            self._json({"error": str(e)}, 500)
            return
        token = _create_session()
        resp = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Set-Cookie", self._session_cookie(token))
        self.end_headers()
        self.wfile.write(resp)
        print(f"[Auth] Initial password set for user '{username}'")

    def _handle_change_password(self):
        if not BCRYPT_AVAILABLE or not _bcrypt:
            self._json({"error": "bcrypt not installed on server"}, 500)
            return
        try:
            body = json.loads(self._read_body() or b"{}")
        except Exception:
            self._json({"error": "Bad request"}, 400)
            return
        password = body.get("password", "")
        if len(password) < 8:
            self._json({"error": "Password must be at least 8 characters"}, 400)
            return
        try:
            pw_hash = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
            from auth import _load_auth
            auth_data = _load_auth()
            username = auth_data.get("username", "admin")
            _save_auth(username, pw_hash)
        except Exception as e:
            self._json({"error": str(e)}, 500)
            return
        self._json({"ok": True})
        print(f"[Auth] Password changed for user '{username}'")

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
    # CC1: http://{ip}:80/thumbnail/{bare_filename}  (no auth needed)
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

        # CC1: dedicated /thumbnail/{bare_filename} endpoint (no auth required)
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
        # Auth-exempt routes
        if self.path == "/cert.pem":
            data = CERT_FILE.read_bytes() if CERT_FILE.exists() else b""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-pem-file")
            self.send_header("Content-Disposition", 'attachment; filename="spooler-cert.pem"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path in ("/login", "/login.html"):
            login_file = Path(__file__).parent / "public" / "login.html"
            data = login_file.read_bytes() if login_file.exists() else b"Login page not found"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
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
            # Liveness probe for supervisor.py — unauthenticated, so it reveals nothing but up/down.
            ok = health.event_loop_responsive()
            self._json({"status": "ok" if ok else "unresponsive", "uptime_s": health.uptime_s()},
                       200 if ok else 503)
            return

        if self.path == "/api/auth-status":
            resp = {"setup_required": not _has_password()}
            if _auth_ok(self):
                resp["spoolman_url"] = "/spoolman/" if PROXY_SPOOLMAN else get_spoolman_url() + "/"
            self._json(resp)
            return

        if self.path == "/api/push-public-key":
            # Public key is safe to expose without auth — it's literally a public key.
            # Service workers need it to renew expired subscriptions without a session cookie.
            if WEBPUSH_AVAILABLE and get_public_key():
                self._json({"publicKey": get_public_key()})
            else:
                self._json({"error": "Web Push not available"}, 503)
            return

        if self.path == "/api/notification-settings":
            if not self._check_auth():
                return
            self._json(load_notif_settings())
            return

        if not self._check_auth():
            return

        if self.path.startswith("/api/camera/"):
            self._proxy_camera()
        elif self.path.startswith("/api/thumbnail/"):
            self._proxy_thumbnail()
        elif self.path == "/api/printers":
            self._json([p.to_dict() for p in state.printers.values()])
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
        elif self.path == "/api/slices":
            self._json({"slices": slicer.list_slices()})
        elif self.path == "/api/splits":
            self._json({"splits": splits.list_splits()})
        elif self.path == "/api/splits/status":
            self._json(splits.status())
        elif self.path.startswith("/api/splits/"):
            self._handle_splits_get()
        elif self.path == "/api/projects":
            self._json({"projects": projects.list_projects()})
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
        elif self.path.startswith("/api/printers/") and self.path.endswith("/hardware"):
            self._jobs_call(lambda: preflight.printer_hardware(self._path_printer_id()), broadcast=False)
        elif self.path == "/api/prints" or self.path.startswith("/api/prints?"):
            self._jobs_call(lambda: print_history.search(**self._print_filters(paged=True)), broadcast=False)
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
            and self.path not in ("/", "")
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
        if not self._check_auth():
            return
        if self.path.startswith("/api/jobs/"):
            self._jobs_call(lambda: jobs.update_job(self._job_id(), self._json_body()))
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
        if not self._check_auth():
            return
        if self.path.startswith("/api/jobs/"):
            self._jobs_call(lambda: jobs.delete_job(self._job_id()))
            return
        if self.path.startswith("/api/slices/"):
            self._slices_call(lambda: slicer.delete_slice(self._job_id()))
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
            if len(parts) == 6 and parts[4] == "models" and parts[5].isdigit():
                self._jobs_call(lambda: projects.delete_model(self._job_id(), int(parts[5])), broadcast=False)
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
        # Auth-exempt
        if self.path == "/api/login":
            self._handle_login()
            return
        if self.path == "/api/logout":
            self._handle_logout()
            return
        if self.path == "/api/setup":
            self._handle_setup()
            return

        if not self._check_auth():
            return

        if self.path == "/api/change-password":
            self._handle_change_password()
        elif self.path == "/api/push-subscribe":
            self._handle_push_subscribe()
        elif self.path == "/api/push-unsubscribe":
            self._handle_push_unsubscribe()
        elif self.path == "/api/notification-settings":
            self._handle_notif_settings()
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
        elif self.path.startswith("/api/slices/") or self.path in ("/api/slicer/profiles", "/api/slicer/presets", "/api/slicer/presets/import", "/api/slicer/spool-match"):
            self._handle_slicer_post()
        elif self.path.startswith("/api/splits/"):
            self._handle_splits_post()
        elif self.path == "/api/projects" or self.path.startswith("/api/projects/"):
            self._handle_projects_post()
        elif self.path.startswith("/api/printers/") and self.path.endswith("/hardware"):
            self._jobs_call(lambda: preflight.set_printer_hardware(
                self._path_printer_id(), self._json_body().get("nozzle_diameter")), broadcast=False)
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
            self._json({"error": str(e)}, e.status)
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

    def _slices_call(self, fn) -> None:
        self._jobs_call(fn, broadcast=False)
        slicer.broadcast_threadsafe()

    def _handle_slicer_post(self):
        path = self.path.split("?", 1)[0]
        parts = path.split("/")  # ['', 'api', 'slices', <id>, <action>]
        if path == "/api/slices/upload":
            self._handle_slice_upload()
        elif path == "/api/slicer/profiles":
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
        elif len(parts) == 5 and parts[2] == "slices" and parts[4] == "retry":
            self._slices_call(lambda: slicer.retry_slice(self._job_id()))
        else:
            self._json({"error": "Not found"}, 404)

    def _handle_slice_upload(self):
        # The body is the raw model file, streamed to disk; options travel in the query string.
        self.close_connection = True  # an early rejection leaves the body unread
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        def value(key: str) -> str:
            return (query.get(key) or [""])[0]

        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        options = {"leveling": value("leveling") in ("1", "true"), "timelapse": value("timelapse") in ("1", "true")}
        self._slices_call(lambda: slicer.create_slice(
            self.rfile, length, value("filename"), value("bundle_id"), bed=value("bed") or "textured",
            printer_id=value("printer_id") or None, print_opts=options, notes=value("notes")))

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

    def _splits_call(self, fn, *, queue: bool = False) -> None:
        self._jobs_call(fn, broadcast=False)
        splits.broadcast_threadsafe()
        if queue:
            slicer.broadcast_threadsafe()

    def _handle_splits_get(self):
        parts = self.path.split("?", 1)[0].split("/")  # ['', 'api', 'splits', <id>, <kind>, <file>]
        try:
            split_id = self._job_id()
            if len(parts) == 4:
                self._json(splits.get_split(split_id, detail=True))
            elif len(parts) == 5 and parts[4] in ("preview", "parts-preview"):
                self._send_file(splits.file_path(split_id, parts[4]), "application/octet-stream")
            elif len(parts) == 6 and parts[4] == "parts" and parts[5].endswith(".stl") and parts[5][:-4].isdigit():
                path, name = splits.part_path(split_id, int(parts[5][:-4]))
                self._send_file(path, "model/stl", download_name=name)
            else:
                self._json({"error": "Not found"}, 404)
        except jobs.JobError as e:
            self._json({"error": str(e)}, e.status)

    def _send_bytes(self, body: bytes, content_type: str, download_name: str | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
        }
        action = parts[4] if len(parts) == 5 else None
        if action not in actions:
            self._json({"error": "Not found"}, 404)
            return
        self._splits_call(actions[action], queue=action == "queue")

    # ── Slicer workspace projects ─────────────────────────────────────────────

    def _handle_projects_get(self):
        parts = self.path.split("?", 1)[0].split("/")  # ['', 'api', 'projects', <id>, <section>, <item id>, <what>]
        try:
            project_id = self._job_id()
            if len(parts) == 4:
                self._json(projects.get_project(project_id))
            elif len(parts) == 5 and parts[4] == "jobs":
                self._json({"jobs": projects.project_jobs(project_id)})
            elif len(parts) == 5 and parts[4] == "export.3mf":
                record = projects.get_project(project_id)
                name = re.sub(r'[^\w .-]+', "_", record["name"]).strip() or "project"
                self._send_bytes(projects.export_orca_project(project_id), "model/3mf", download_name=f"{name}.3mf")
            elif len(parts) == 7 and parts[4] == "models" and parts[6] == "preview":
                self._send_file(projects.model_preview(project_id, int(parts[5])), "application/octet-stream")
            elif len(parts) == 7 and parts[4] == "plates" and parts[6] == "fit":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                self._json(farm.plate_fit(projects.get_project(project_id), int(parts[5]), (query.get("printer_id") or [None])[0]))
            elif len(parts) == 7 and parts[4] == "plates" and parts[6] == "analysis":
                self._json(projects.plate_analysis(project_id, int(parts[5])))
            elif len(parts) == 7 and parts[4] == "plates" and parts[6] in ("toolpath", "gcode"):
                plate_id = int(parts[5])
                path = projects.plate_file(project_id, plate_id, parts[6])
                if parts[6] == "gcode":
                    record = projects.get_project(project_id)
                    plate = next((p for p in record["data"]["plates"] if p["id"] == plate_id), {"name": f"Plate {plate_id}"})
                    name = re.sub(r'[^\w .-]+', "_", f"{record['name']}_{plate['name']}").strip() + ".gcode"
                    self._send_file(path, "text/plain; charset=utf-8", download_name=name)
                else:
                    self._send_file(path, "application/octet-stream")
            else:
                self._json({"error": "Not found"}, 404)
        except ValueError:
            self._json({"error": "Not found"}, 404)
        except jobs.JobError as e:
            self._json({"error": str(e)}, e.status)

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
                return projects.create_project(body.get("name", ""), split_id=split_id)
            self._jobs_call(create, broadcast=False)
            return
        if path == "/api/projects/upload":
            self._handle_project_upload(new_project=True)
            return
        if len(parts) == 5 and parts[4] in ("layout", "whole", "pack", "slice", "queue"):
            actions = {
                "layout": lambda: projects.lay_out_split(self._job_id(), self._json_body().get("parts")),
                "whole":  lambda: projects.place_whole_model(self._job_id()),
                "pack":   lambda: projects.pack_plates(self._job_id(), self._json_body().get("plates")),
                "slice":  lambda: (lambda body: projects.request_slices(self._job_id(), body.get("plates"), bool(body.get("send"))))(self._json_body()),
                "queue":  lambda: projects.queue_plates(self._job_id(), self._json_body()),
            }
            self._jobs_call(actions[parts[4]], broadcast=parts[4] == "queue")
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
        if len(parts) == 7 and parts[4] == "plates" and parts[5].isdigit() and parts[6] in ("arrange", "slice", "queue", "merge"):
            plate_id, action = int(parts[5]), parts[6]

            def merge():
                try:
                    into = int(self._json_body().get("into"))
                except (TypeError, ValueError):
                    raise jobs.JobError("into must be a plate number")
                return projects.merge_plates(self._job_id(), plate_id, into)
            actions = {
                "arrange": lambda: projects.arrange_plate(self._job_id(), plate_id),
                "slice":   lambda: projects.request_slice(self._job_id(), plate_id, bool(self._json_body().get("send"))),
                "queue":   lambda: projects.queue_plate(self._job_id(), plate_id, self._json_body()),
                "merge":   merge,
            }
            self._jobs_call(actions[action], broadcast=action == "queue")
            return
        self._json({"error": "Not found"}, 404)

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
                                                                          bed=value("bed") or "textured"))
            return
        self._jobs_call(lambda: projects.import_model(self._job_id(), self.rfile, length, value("filename"),
                                                     scale=value("scale") or 1.0, up=value("up") or "z",
                                                     plate_id=value("plate") or None), broadcast=False)

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

        filters = {"q": text("q"), "printer_id": text("printer_id") or None,
                   "outcome": text("outcome") or None, "since": number("since"), "until": number("until")}
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


def run_http(port: int) -> None:
    server = ThreadingHTTPServer(("0.0.0.0", port), SPHandler)
    print(f"[HTTP] Serving on http://0.0.0.0:{port}")
    server.serve_forever()


def run_https(port: int) -> None:
    if not ensure_ssl_cert():
        return
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    server = ThreadingHTTPServer(("0.0.0.0", port), SPHandler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    server._is_https = True
    print(f"[HTTPS] Serving on https://0.0.0.0:{port}")
    server.serve_forever()
