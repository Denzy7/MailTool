"""The MailTool web server: standard library only (http.server), one process,
one shared session that any browser with the access code can use.

    mailtool --web                      all interfaces, port 8765, random access code
    mailtool --web --port 9000 --host 127.0.0.1
    MAILTOOL_WEB_TOKEN=secret mailtool --web
    mailtool --web --cert cert.pem --key key.pem      HTTPS
"""
from __future__ import annotations

import hmac
import json
import os
import queue
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mailtool import APP_NAME, __version__
from mailtool.core.util import asset, log
from mailtool.web.api import Api
from mailtool.web.keys import FILE_NAME as KEYS_FILE, LoginKeys
from mailtool.web.state import ApiError, WebState
from mailtool.web.webio import FileResponse, Request, Response, json_response

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
COOKIE = "mailtool_sid"
SESSION_TTL = 30 * 86400
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"    # no 0/O, 1/I
UNSAFE = ("POST", "PUT", "PATCH", "DELETE")
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
       "frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
                ".ico": "image/x-icon", ".json": "application/json"}


def new_code():
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return raw[:4] + "-" + raw[4:]


def norm_code(c):
    return re.sub(r"[\s-]", "", str(c or "")).upper()


class Auth:
    """Access code or login file -> session cookie, with a brake on guessing."""

    def __init__(self, code, keys=None):
        self.code = code
        self._want = norm_code(code)
        self.keys = keys            # LoginKeys, or None to allow the access code only
        self.sessions = {}          # sid -> {"exp": time, "via": "code" | "key", "kid": key id or None}
        self.fails = {}
        self.lock = threading.Lock()

    def _throttle(self, ip):
        with self.lock:
            n, until = self.fails.get(ip, (0, 0))
        if until > time.time():
            raise ApiError(429, "Too many failed sign-ins - wait %d seconds." % int(until - time.time() + 1))

    def _failed(self, ip, message):
        now = time.time()
        with self.lock:
            n = self.fails.get(ip, (0, 0))[0] + 1
            self.fails[ip] = (n, now + min(300, 2 ** max(0, n - 3)) if n >= 3 else 0)
        time.sleep(0.5)
        raise ApiError(403, message)

    def _session(self, ip, via, kid=None):
        sid = secrets.token_urlsafe(32)
        with self.lock:
            self.fails.pop(ip, None)
            self.sessions[sid] = {"exp": time.time() + SESSION_TTL, "via": via, "kid": kid}
        return sid

    def check_code(self, ip, code):
        self._throttle(ip)
        if hmac.compare_digest(norm_code(code).encode(), self._want.encode()):
            return self._session(ip, "code")
        self._failed(ip, "Wrong access code.")

    def check_key(self, ip, key):
        self._throttle(ip)
        kid = self.keys.check(key) if self.keys is not None else None
        if kid:
            return self._session(ip, "key", kid)
        self._failed(ip, "That login file is not valid here (revoked, or for another MailTool server).")

    def session(self, sid):
        if not sid:
            return None
        with self.lock:
            s = self.sessions.get(sid)
            if s is None:
                return None
            if s["exp"] < time.time():
                del self.sessions[sid]
                return None
        if s["kid"] and (self.keys is None or not self.keys.exists(s["kid"])):
            self.logout(sid)          # its login file was revoked
            return None
        return s

    def valid(self, sid):
        return self.session(sid) is not None

    def logout(self, sid):
        with self.lock:
            self.sessions.pop(sid, None)

    def end_key_sessions(self, kid):
        with self.lock:
            for sid in [k for k, v in self.sessions.items() if v["kid"] == kid]:
                del self.sessions[sid]


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, addr, handler, state, api, auth, tls=None):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        self.state, self.auth, self.tls = state, auth, tls
        self.routes = [(m, re.compile(p + r"\Z"), fn) for m, p, fn in api.routes()]
        self.stopping = threading.Event()
        super().__init__(addr, handler)

    def finish_request(self, request, client_address):
        # TLS handshake here, on the request's own thread, so a slow client can't stall accept()
        if self.tls is not None:
            try:
                request = self.tls.wrap_socket(request, server_side=True)
            except (ssl.SSLError, OSError) as e:
                log.debug("TLS handshake failed from %s: %s", client_address[0], e)
                return
        super().finish_request(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "%s/%s" % (APP_NAME, __version__)
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug("web %s - %s", self.address_string(), fmt % args)

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("HEAD")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    # ------------------------------------------------------------------ plumbing
    @property
    def https(self):
        return self.server.tls is not None

    def cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def authed(self):
        return self.server.auth.valid(self.cookie(COOKIE))

    def same_origin(self):
        """Unsafe requests must come from our own page: a custom header (a cross-site page can't send one
        without a CORS preflight we never answer) and, if the browser says, our own Origin."""
        if self.headers.get("X-MailTool") != "1":
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            host = self.headers.get("Host") or ""
            if urllib.parse.urlsplit(origin).netloc != host:
                return False
        return True

    def dispatch(self, method):
        req = None
        try:
            parts = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parts.path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query, keep_blank_values=True).items()}
            resp = self.route(method, path, query)
            if isinstance(resp, Request):     # SSE took over the connection
                return
        except ApiError as e:
            resp = json_response(dict(e.extra, error=e.message), e.status)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            return
        except Exception as e:
            log.exception("web request %s %s failed", method, self.path)
            resp = json_response({"error": "Server error: %s" % e}, 500)
        req = getattr(self, "_req", None)
        n = self.headers.get("Content-Length")
        if n and n != "0" and (req is None or not req._body_read):
            self.close_connection = True     # don't read an unwanted body (e.g. a refused upload)
        try:
            self.send(resp, head=(method == "HEAD"))
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            self.close_connection = True

    def route(self, method, path, query):
        self._req = None
        # ---- public: the page itself, its assets, and logging in
        if method in ("GET", "HEAD"):
            if path in ("/", "/index.html"):
                return FileResponse(os.path.join(STATIC_DIR, "index.html"), ctype=STATIC_TYPES[".html"],
                                    inline=True)
            if path.startswith("/static/"):
                return self.static(path[len("/static/"):])
            if path.startswith("/assets/"):
                name = os.path.basename(path)
                p = asset(name)
                if re.match(r"^[\w.-]+\.(png|ico)$", name) and os.path.isfile(p):
                    return FileResponse(p, inline=True, cache=True)
                raise ApiError(404, "Not found.")
            if path == "/favicon.ico":
                return FileResponse(asset("icon_32.png"), ctype="image/png", inline=True, cache=True)
        if method in UNSAFE and not self.same_origin():
            raise ApiError(403, "Cross-site request refused.")
        if path == "/login" and method == "POST":
            self._req = Request(self, method, path, query, ())
            d = self._req.json()
            ip = self.client_address[0]
            if d.get("key"):
                sid, via = self.server.auth.check_key(ip, d.get("key")), "key"
            else:
                sid, via = self.server.auth.check_code(ip, d.get("code")), "code"
            resp = json_response({"ok": True, "via": via})
            resp.headers["Set-Cookie"] = "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s" % (
                COOKIE, sid, SESSION_TTL, "; Secure" if self.https else "")
            return resp
        if path == "/logout" and method == "POST":
            self.server.auth.logout(self.cookie(COOKIE))
            resp = json_response({"ok": True})
            resp.headers["Set-Cookie"] = "%s=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" % COOKIE
            return resp
        if path == "/api/ping":
            sess = self.server.auth.session(self.cookie(COOKIE))
            return json_response({"ok": True, "authed": sess is not None, "via": sess and sess["via"],
                                  "app": APP_NAME, "version": __version__})

        # ---- everything below needs a session
        if not self.authed():
            raise ApiError(401, "Enter the access code.", need="login")
        if path == "/events" and method == "GET":
            self.events()
            return Request(self, method, path, query, ())
        m = re.match(r"^/files/([\w-]+)/", path)
        if m and method in ("GET", "HEAD"):
            f = self.server.state.files.get(m.group(1))
            if f is None:
                raise ApiError(404, "That download has expired - make it again.")
            return FileResponse(f["path"], name=f["name"], ctype=f["ctype"], inline=query.get("download") != "1")
        if path == "/api/login-keys":
            keys = self.server.auth.keys
            if method == "GET":
                return json_response({"keys": keys.list() if keys else [], "enabled": keys is not None,
                                      "current": (self.server.auth.session(self.cookie(COOKIE)) or {}).get("kid")})
            if method == "POST":
                if keys is None:
                    raise ApiError(409, "Login files are switched off on this server.")
                self._req = Request(self, method, path, query, ())
                info, key = keys.create(self._req.json().get("label"))
                self.server.state.log("Login file created for %s." % info["label"], "ok", prefix="Web")
                return json_response({"key": key, "info": info})
        m = re.match(r"^/api/login-keys/(\w+)$", path)
        if m and method == "DELETE":
            keys = self.server.auth.keys
            if keys is None or not keys.revoke(m.group(1)):
                raise ApiError(404, "No such login file.")
            self.server.auth.end_key_sessions(m.group(1))
            self.server.state.log("A login file was revoked.", "ok", prefix="Web")
            return json_response({"ok": True})
        want = "GET" if method == "HEAD" else method
        allowed = False
        for meth, rx, fn in self.server.routes:
            mm = rx.match(path)
            if not mm:
                continue
            allowed = True
            if meth != want:
                continue
            self._req = Request(self, method, path, query, mm.groups())
            out = fn(self._req)
            if isinstance(out, (Response, FileResponse)):
                return out
            return json_response(out)
        if allowed:
            raise ApiError(405, "Method not allowed.")
        raise ApiError(404, "Not found.")

    def static(self, rel):
        rel = rel.replace("\\", "/")
        if not re.match(r"^[\w.-]+(/[\w.-]+)*$", rel) or ".." in rel.split("/"):
            raise ApiError(404, "Not found.")
        p = os.path.join(STATIC_DIR, *rel.split("/"))
        if not os.path.isfile(p):
            raise ApiError(404, "Not found.")
        return FileResponse(p, ctype=STATIC_TYPES.get(os.path.splitext(p)[1], "application/octet-stream"),
                            inline=True)

    def common_headers(self, html=False):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy", CSP if html else "default-src 'none'; frame-ancestors 'self'; "
                                                                     "img-src 'self'; style-src 'unsafe-inline'")

    def send(self, resp, head=False):
        if isinstance(resp, FileResponse):
            return self.send_file(resp, head)
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.ctype)
        self.send_header("Content-Length", str(len(resp.body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in resp.headers.items():
            self.send_header(k, v)
        self.common_headers()
        self.end_headers()
        if not head:
            self.wfile.write(resp.body)

    def send_file(self, f, head=False):
        try:
            size = os.path.getsize(f.path)
            fh = open(f.path, "rb")
        except OSError:
            return self.send(json_response({"error": "File not found."}, 404), head)
        with fh:
            self.send_response(200)
            self.send_header("Content-Type", f.ctype)
            self.send_header("Content-Length", str(size))
            is_page = f.ctype.startswith("text/html") or f.ctype.startswith("text/javascript") or \
                f.ctype.startswith("text/css")
            if not is_page:
                self.send_header("Content-Disposition", f.disposition())
            self.send_header("Cache-Control", "private, max-age=3600" if f.cache else "no-store")
            if f.ctype == "application/pdf":
                # no CSP: browsers' PDF viewers may refuse to load under one; PDFs can't run script on our origin
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "SAMEORIGIN")
            else:
                self.common_headers(html=is_page)
            self.end_headers()
            if head:
                return
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    # ------------------------------------------------------------------ server-sent events
    def events(self):
        hub = self.server.state.hub
        q = hub.subscribe()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.common_headers()
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while not self.server.stopping.is_set():
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                data = json.dumps(ev, ensure_ascii=False, default=str)
                self.wfile.write(("data: %s\n\n" % data).encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            pass
        finally:
            hub.unsubscribe(q)


# ============================================================================== start-up
def lan_addresses():
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))       # no packet is sent; just picks the outgoing interface
            out.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out and not ip.startswith("127."):
                out.append(ip)
    except OSError:
        pass
    return out


def make_server(host="0.0.0.0", port=8765, code=None, cert=None, key=None, state=None, max_upload_mb=512,
                config=None, login_files=True):
    state = state or WebState(config=config, max_upload_mb=max_upload_mb)
    tls = None
    if cert:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(cert, key or None)
    keys = LoginKeys(os.path.join(os.path.dirname(state.cfg.path), KEYS_FILE)) if login_files else None
    auth = Auth(code or new_code(), keys)
    return Server((host, int(port)), Handler, state, Api(state), auth, tls)


def serve(host="0.0.0.0", port=8765, code=None, cert=None, key=None, max_upload_mb=512, config=None):
    code = code or os.environ.get("MAILTOOL_WEB_TOKEN") or None
    try:
        srv = make_server(host, port, code, cert, key, max_upload_mb=max_upload_mb, config=config)
    except OSError as e:
        sys.stderr.write("Cannot listen on %s:%s - %s\n" % (host, port, e))
        return 2
    except (ssl.SSLError, FileNotFoundError) as e:
        sys.stderr.write("Cannot load the TLS certificate: %s\n" % e)
        return 2
    scheme = "https" if srv.tls else "http"
    real_port = srv.server_address[1]
    hosts = lan_addresses() if host in ("0.0.0.0", "::", "") else [host]
    print("%s %s web server" % (APP_NAME, __version__), flush=True)
    print("  open:        %s" % "\n               ".join("%s://%s:%d/" % (scheme, h, real_port)
                                                         for h in (["localhost"] if host in ("0.0.0.0", "::", "")
                                                                   else []) + hosts), flush=True)
    print("  access code: %s" % srv.auth.code, flush=True)
    print("  settings:    %s" % srv.state.cfg.path, flush=True)
    if srv.auth.keys is not None:
        print("  login files: %d active (revoke them in Settings › General)" % len(srv.auth.keys.list()),
              flush=True)
    if not srv.tls and host not in ("127.0.0.1", "localhost", "::1"):
        print("  note:        plain HTTP - the access code and mail password cross the network unencrypted.\n"
              "               Use --cert/--key (or a reverse proxy with HTTPS) outside a trusted LAN.", flush=True)
    print("  Ctrl+C stops the server.", flush=True)
    srv.state.log("Web server started on port %d." % real_port, "ok", prefix="Web")
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping…", flush=True)
    finally:
        srv.stopping.set()
        srv.server_close()
        srv.state.close()
    return 0
