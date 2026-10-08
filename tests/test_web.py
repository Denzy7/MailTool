"""The web server end to end: a real server on a random local port, the fake
mailbox for fetch/sort, and a stub printer (nothing is ever sent to CUPS)."""
import http.client
import json
import os
import threading
import time
import urllib.parse

import pytest

from fakes import make_pdf
from test_pipeline import install_fake_mailbox
from mailtool.core import secrets
from mailtool.core.config import Config
from mailtool.web import api as web_api
from mailtool.web.server import make_server, norm_code
from mailtool.web.state import WebState
from mailtool.web.webio import FileResponse

CODE = "TEST-CODE"


class StubBackend:
    name = "stub"

    def __init__(self):
        self.jobs = []

    def printers(self):
        return ["Office", "PDF"], "Office"

    def submit(self, pdf, printer, copies, title):
        with open(pdf, "rb") as f:
            self.jobs.append({"printer": printer, "copies": copies, "title": title, "head": f.read(5)})
        return "stub-%d" % len(self.jobs)

    def cancel(self, ids):
        pass


class Client:
    def __init__(self, port):
        self.port = port
        self.cookie = None

    def req(self, method, path, body=None, raw=None, headers=None, csrf=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = dict(headers or {})
        if csrf:
            h["X-MailTool"] = "1"
        if self.cookie:
            h["Cookie"] = self.cookie
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        payload = r.read()
        sc = r.getheader("Set-Cookie")
        if sc:
            self.cookie = sc.split(";", 1)[0]
        c.close()
        r.payload = payload
        try:
            r.data = json.loads(payload.decode()) if payload and "json" in (r.getheader("Content-Type") or "") else None
        except ValueError:
            r.data = None
        return r

    def ok(self, method, path, body=None, **kw):
        r = self.req(method, path, body, **kw)
        assert 200 <= r.status < 300, (r.status, r.payload[:300])
        return r.data

    def login(self):
        return self.ok("POST", "/login", {"code": CODE})

    def wait_job(self, jid, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            j = self.ok("GET", "/api/jobs/%d" % jid)
            if j["state"] != "running":
                return j
            time.sleep(0.1)
        raise AssertionError("job %s still running" % jid)

    def wait_queue(self, n, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            q = self.ok("GET", "/api/queue")
            if len(q["items"]) >= n and all(i["status"] not in ("queued", "staging", "converting") for i in q["items"]):
                return q
            time.sleep(0.1)
        raise AssertionError("queue not ready: %s" % q)

    def upload(self, name, data):
        return self.ok("PUT", "/api/upload?name=" + urllib.parse.quote(name), raw=data,
                       headers={"Content-Type": "application/octet-stream"})


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets, "keyring_available", lambda: False)   # never touch the real keyring
    cfg = Config(str(tmp_path / "settings.json"))
    cfg.set("general", "library_dir", str(tmp_path / "lib"))
    cfg.update("print", {"temp_dir": str(tmp_path / "ptmp")})
    stub = StubBackend()
    from mailtool.web import printsvc
    monkeypatch.setattr(printsvc, "get_backend", lambda pcfg: stub)     # also after settings are saved
    state = WebState(config=cfg, max_upload_mb=5)
    srv = make_server("127.0.0.1", 0, CODE, state=state)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    srv.stub = stub
    yield srv
    srv.stopping.set()
    srv.shutdown()
    srv.server_close()
    state.close()


@pytest.fixture
def client(server):
    c = Client(server.server_address[1])
    return c


# ------------------------------------------------------------------ auth & safety
def test_login_and_csrf(client):
    assert client.ok("GET", "/api/ping")["authed"] is False
    r = client.req("GET", "/api/state")
    assert r.status == 401 and r.data["need"] == "login"
    assert client.req("POST", "/login", {"code": "nope"}).status == 403
    assert client.req("POST", "/login", {"code": "test code"}, csrf=False).status == 403   # no X-MailTool header
    client.login()                                    # code is case/dash-insensitive
    assert client.ok("GET", "/api/ping")["authed"] is True
    st = client.ok("GET", "/api/state")
    assert st["app"] == "MailTool" and st["queue"]["items"] == []
    # an unsafe request without our header, or from another origin, is refused even with the cookie
    assert client.req("DELETE", "/api/queue", csrf=False).status == 403
    assert client.req("DELETE", "/api/queue", headers={"Origin": "http://evil.example"}).status == 403
    assert client.req("DELETE", "/api/queue").status == 200
    client.ok("POST", "/logout")
    assert client.req("GET", "/api/state").status == 401


def test_code_normalising():
    assert norm_code(" test-code ") == norm_code("TESTCODE")


def test_page_and_static_are_public_with_csp(client):
    r = client.req("GET", "/")
    assert r.status == 200 and b"/static/app.js" in r.payload
    assert "script-src 'self'" in r.getheader("Content-Security-Policy")
    assert client.req("GET", "/static/app.js").status == 200
    assert client.req("GET", "/static/../server.py").status == 404
    assert client.req("GET", "/assets/icon_32.png").getheader("Content-Type") == "image/png"


def test_unsafe_file_types_download_instead_of_rendering(tmp_path):
    p = tmp_path / "x.html"
    p.write_text("<script>alert(1)</script>")
    f = FileResponse(str(p))
    assert not f.inline and f.disposition().startswith("attachment")
    assert FileResponse(str(p), inline=True).inline is False
    assert FileResponse(str(tmp_path / "a.pdf")).inline is True


def test_inside():
    assert web_api.inside("/a/b", "/a/b/c.txt")
    assert not web_api.inside("/a/b", "/a/b/../c.txt")
    assert not web_api.inside("/a/b", "/a/bc/d")


# ------------------------------------------------------------------ print
def test_upload_queue_download_and_server_print(client, server, tmp_path):
    client.login()
    first = make_pdf("first")
    client.upload("one.pdf", first)
    client.upload("../../evil two.pdf", make_pdf("second page"))             # odd name
    client.upload("dup.pdf", first)                                          # same bytes as one.pdf
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), "teal").save(buf, "PNG")
    client.upload("pic.png", buf.getvalue())
    q = client.wait_queue(3)
    names = [i["name"] for i in q["items"]]
    assert names == ["one.pdf", "evil two.pdf", "pic.png"], q      # dup dropped, path parts stripped
    assert all(i["status"] == "ready" and i["pages"] == 1 for i in q["items"]), q
    for d in os.listdir(server.state.prints.upload_root):
        for name in os.listdir(os.path.join(server.state.prints.upload_root, d)):
            assert "/" not in name and not name.startswith(".")

    ids = [i["id"] for i in q["items"]]
    client.ok("POST", "/api/queue/order", {"ids": [ids[2], ids[0], ids[1]]})
    assert [i["id"] for i in client.ok("GET", "/api/queue")["items"]] == [ids[2], ids[0], ids[1]]
    assert client.ok("PATCH", "/api/queue/" + ids[0], {"copies": 2})["copies"] == 2
    assert client.req("PATCH", "/api/queue/" + ids[0], {"excluded": [0]}).status == 400   # can't drop every page

    r = client.req("GET", "/api/queue/%s/thumb/0?w=120" % ids[0])
    assert r.status == 200 and r.payload[:4] == b"\x89PNG"
    r = client.req("GET", "/api/queue/%s/pdf" % ids[1])
    assert r.status == 200 and r.payload[:4] == b"%PDF"

    job = client.ok("POST", "/api/print/download")["job"]
    j = client.wait_job(job["id"])
    assert j["state"] == "done", j
    res = j["result"]
    assert res["kind"] == "download" and res["files"] == 3 and res["pages"] == 4      # one.pdf twice
    r = client.req("GET", res["url"] + "?download=1")
    assert r.status == 200 and r.payload[:4] == b"%PDF"
    assert r.getheader("Content-Disposition").startswith("attachment")
    assert server.stub.jobs == []                                                   # nothing printed

    assert client.ok("GET", "/api/printers")["names"] == ["Office", "PDF"]
    job = client.ok("POST", "/api/print/server", {"printer": "PDF", "merged": False})["job"]
    j = client.wait_job(job["id"])
    assert j["result"]["printed"] == 3, j
    assert [(p["printer"], p["copies"]) for p in server.stub.jobs] == [("PDF", 1), ("PDF", 2), ("PDF", 1)]
    assert all(p["head"] == b"%PDF-" for p in server.stub.jobs)
    q = client.ok("GET", "/api/queue")
    assert {i["status"] for i in q["items"]} == {"done"} and q["busy"] is None

    client.ok("DELETE", "/api/queue")
    assert client.ok("GET", "/api/queue")["items"] == []
    assert client.req("GET", res["url"]).status == 404          # download links go with the queue


def test_upload_limits(client):
    client.login()
    # refused from the declared size alone, before any of the body is read
    c = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    c.putrequest("PUT", "/api/upload?name=big.pdf")
    for k, v in (("X-MailTool", "1"), ("Cookie", client.cookie), ("Content-Length", str(6 * 1024 * 1024))):
        c.putheader(k, v)
    c.endheaders()
    r = c.getresponse()
    assert r.status == 413 and b"limit 5 MB" in r.read()
    c.close()
    assert client.ok("GET", "/api/state")["max_upload"] == 5 * 1024 * 1024
    assert client.req("POST", "/api/print/download").status == 400     # empty queue


# ------------------------------------------------------------------ fetch / library / sort
def setup_account(client, tmp_path):
    client.ok("PUT", "/api/settings", {
        "account": {"server": "imap.test", "port": 993, "username": "me@test", "mailbox": "INBOX", "use_ssl": True},
        "general": {"library_dir": str(tmp_path / "lib"), "timezone": "Africa/Nairobi", "theme": "dark"},
        "emailinfo": {"body_mode": "text", "workers": 1, "header_right": "EMAIL UID: <UID>"},
    })


def test_fetch_library_sort_flow(client, server, tmp_path, monkeypatch):
    install_fake_mailbox(monkeypatch.setattr)
    client.login()
    setup_account(client, tmp_path)
    body = {"from_date": "2026-10-06", "from_time": "00:00", "to_date": "2026-10-06", "to_time": "23:59",
            "save_attachments": True, "emailinfo": True, "export_csv": True}
    r = client.req("POST", "/api/fetch", body)
    assert r.status == 409 and r.data["need"] == "password"            # asked for, never stored in settings
    client.ok("POST", "/api/password", {"password": "pw", "remember": False})
    job = client.ok("POST", "/api/fetch", body)["job"]
    j = client.wait_job(job["id"])
    assert j["state"] == "done", j
    assert j["result"]["messages"] == 3 and j["result"]["pdfs"] == 3
    csv_url = j["result"]["csv"]["url"]
    assert b"Date Received" in client.req("GET", csv_url).payload
    with open(tmp_path / "settings.json", encoding="utf-8") as f:
        saved = json.load(f)
    assert "pw" not in json.dumps(saved["account"]) and saved["fetch"]["from_date"] == "06-Oct-2026"

    lib = client.ok("GET", "/api/library")
    assert lib["stats"]["total"] == 3 and len(lib["rows"]) == 3
    bob = next(r for r in lib["rows"] if r["subject"] == "Scan")
    assert client.ok("GET", "/api/library?q=alice")["rows"][0]["sender_email"] == "alice@a.com"
    detail = client.ok("GET", "/api/library/%d" % bob["id"])
    names = [f["name"] for f in detail["files"]]
    assert names[0].startswith("EMAILINFO") and "scan.pdf" in names
    scan = next(f for f in detail["files"] if f["name"] == "scan.pdf")
    r = client.req("GET", scan["url"])
    assert r.status == 200 and r.payload[:4] == b"%PDF"
    assert client.req("GET", "/api/library/%d/file/a999999" % bob["id"]).status == 404

    # Library › Print: EMAILINFO first, then attachments, MERGED_ copies skipped
    added = client.ok("POST", "/api/library/print", {"ids": [bob["id"]]})
    assert added["added"] == 3, added
    q = client.wait_queue(3)
    assert [i["name"] for i in q["items"]][0].startswith("EMAILINFO")
    assert not any(i["name"].startswith("MERGED_") for i in q["items"])

    # Sort: needs groups first
    r = client.req("POST", "/api/sort/run", {})
    assert r.status == 409 and r.data["need"] == "groups"
    s = client.ok("PUT", "/api/sort", {
        "groups": [{"group_name": "G1/1/1/2026/01", "keywords": ["G1: January", "jan, january"]},
                   {"group_name": "G2/2/1/2026/02", "keywords": "G2: February\nfeb, february"}],
        "excluded": ["disclaimer", "Disclaimer"], "dedupe": True, "input": "library", "library_from": "2026-10-06"})
    assert len(s["groups"]) == 2 and s["excluded"] == ["disclaimer"] and s["library_from"] == "2026-10-06"
    job = client.ok("POST", "/api/sort/run", {})["job"]
    j = client.wait_job(job["id"])
    assert (j["result"]["matched"], j["result"]["unmatched"]) == (2, 1), j
    rep = {x["name"]: x["url"] for x in j["result"]["reports"]}
    assert "matched.csv" in rep and b"G2/2/1/2026/02" in client.req("GET", rep["matched.csv"]).payload
    lib = client.ok("GET", "/api/library?group=__unmatched__")
    assert [r["subject"] for r in lib["rows"]] == ["Misc"]

    # export -> import round trip
    r = client.req("GET", "/api/sort/groups/export")
    data = json.loads(r.payload)
    assert r.getheader("Content-Disposition").startswith("attachment") and len(data["groups"]) == 2
    s = client.ok("POST", "/api/sort/groups/import", {"data": {"groups": [{"group_name": "G3", "keywords": ["x"]}]},
                                                     "replace": False})
    assert [g["group_name"] for g in s["groups"]] == ["G1/1/1/2026/01", "G2/2/1/2026/02", "G3"]

    # sort an uploaded CSV (MailTool's own export: exact UIDs)
    csv_bytes = client.req("GET", csv_url).payload
    up = client.ok("PUT", "/api/sort/csv?name=emails.csv", raw=csv_bytes)
    assert up["csv_name"] == "emails.csv"
    job = client.ok("POST", "/api/sort/run", {})["job"]
    j = client.wait_job(job["id"])
    assert j["state"] == "done" and j["result"]["matched"] == 2, j
    assert j["result"]["out_dir"] == str(tmp_path / "lib" / "reports")      # not next to the temp upload

    client.ok("POST", "/api/library/clear-group", {"ids": [bob["id"]]})
    assert client.ok("GET", "/api/library/%d" % bob["id"])["group"] == ""
    assert client.ok("POST", "/api/library/forget", {"ids": [bob["id"]]})["removed"] == 1
    assert client.ok("GET", "/api/library")["stats"]["total"] == 2


def test_settings_validation(client, tmp_path):
    client.login()
    s = client.ok("PUT", "/api/settings", {"account": {"server": " imap.x ", "username": "u", "port": "99999",
                                                        "timeout": "5"},
                                           "general": {"timezone": "Mars/Base", "theme": "neon"},
                                           "print": {"max_parallel": "1000", "paper": "Tabloid"}})
    assert s["account"]["server"] == "imap.x" and s["account"]["port"] == 65535 and s["account"]["timeout"] == 10
    assert s["general"]["theme"] == "system" and s["warnings"]
    assert client.ok("GET", "/api/tz?name=Africa/Nairobi")["ok"] is True
    assert client.ok("GET", "/api/tz?name=Mars/Base")["ok"] is False
    r = client.req("PUT", "/api/settings", {"print": {"soffice": str(tmp_path / "missing")}})
    assert r.status == 400
    deps = client.ok("GET", "/api/deps")
    assert not any(c["key"] in ("tk", "dnd") for c in deps["caps"])


def test_events_stream(client, server):
    client.login()
    c = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    c.request("GET", "/events", headers={"Cookie": client.cookie})
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/event-stream")
    assert r.fp.readline() == b"retry: 3000\n"
    r.fp.readline()
    server.state.log("hello from the test", "ok")
    line = r.fp.readline()
    ev = json.loads(line.decode()[len("data: "):])
    assert ev["type"] == "log" and ev["text"] == "hello from the test"
    c.close()


def test_job_reported_running_until_its_result_is_ready(server):
    """A job that ends instantly must not show as 'done' before its on-done callback has added the result."""
    state = server.state
    gate = threading.Event()

    def done(job):
        gate.wait(5)
        job.web_result = {"answer": 42}

    job = state.start_job("test", "quick", lambda job: "raw", on_done=done)
    time.sleep(0.3)                              # the worker has finished; the callback is still waiting
    assert job.state == "done"
    from mailtool.web.state import job_json
    assert job_json(job)["state"] == "running" and job_json(job)["result"] is None
    gate.set()
    end = time.time() + 5
    while job_json(job)["state"] == "running" and time.time() < end:
        time.sleep(0.05)
    assert job_json(job)["state"] == "done" and job_json(job)["result"] == {"answer": 42}


def test_config_option(tmp_path, monkeypatch):
    from mailtool.cli import load_config
    default = load_config()
    assert default.path.endswith(os.path.join("MailTool", "settings.json"))
    # a file path that doesn't exist yet: defaults now, created on save (parent folders too)
    p = tmp_path / "web" / "nested" / "mailtool-web.json"
    cfg = load_config(str(p))
    assert cfg.path == str(p) and cfg.get("account", "server") == ""
    cfg.set("account", "server", "imap.web")
    cfg.save()
    assert json.loads(p.read_text())["account"]["server"] == "imap.web"
    assert load_config(str(p)).get("account", "server") == "imap.web"
    # a folder (existing, or written with a trailing slash) means <folder>/settings.json
    assert load_config(str(tmp_path / "web")).path == str(tmp_path / "web" / "settings.json")
    assert load_config(str(tmp_path / "new") + os.sep).path == str(tmp_path / "new" / "settings.json")
    # the web server uses it, and leaves the default file alone
    monkeypatch.setattr(secrets, "keyring_available", lambda: False)
    srv = make_server("127.0.0.1", 0, CODE, config=load_config(str(p)))
    try:
        assert srv.state.cfg.path == str(p) and srv.state.cfg.get("account", "server") == "imap.web"
    finally:
        srv.server_close()
        srv.state.close()
    assert not os.path.exists(default.path) or "imap.web" not in open(default.path).read()


def test_cli_check_reports_settings_file(tmp_path, capsys):
    from mailtool.cli import main
    p = tmp_path / "s.json"
    main(["--check", "--config", str(p)])
    assert "Settings: %s" % p in capsys.readouterr().out


# ------------------------------------------------------------------ login files
def test_login_file_signs_in_survives_restart_and_can_be_revoked(client, server, tmp_path, monkeypatch):
    client.login()
    made = client.ok("POST", "/api/login-keys", {"label": "Phone"})
    key, kid = made["key"], made["info"]["id"]
    assert key.startswith("mtk_") and made["info"]["label"] == "Phone"
    stored = (tmp_path / "web_login_keys.json").read_text()
    assert key not in stored and kid in stored                  # only a hash is kept, next to settings.json
    assert [k["label"] for k in client.ok("GET", "/api/login-keys")["keys"]] == ["Phone"]

    other = Client(client.port)
    assert other.req("POST", "/login", {"key": "mtk_wrong"}).status == 403
    r = other.ok("POST", "/login", {"key": key})
    assert r["via"] == "key"
    assert other.ok("GET", "/api/login-keys")["current"] == kid

    # a restarted server with a different access code still accepts the file
    cfg = Config(str(tmp_path / "settings.json"))
    state2 = WebState(config=cfg, max_upload_mb=5)
    srv2 = make_server("127.0.0.1", 0, "OTHER-CODE", state=state2)
    t = threading.Thread(target=srv2.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    try:
        c2 = Client(srv2.server_address[1])
        assert c2.req("POST", "/login", {"code": CODE}).status == 403
        assert c2.ok("POST", "/login", {"key": key})["via"] == "key"
        assert c2.ok("GET", "/api/state")["app"] == "MailTool"
    finally:
        srv2.shutdown()
        srv2.server_close()
        state2.close()

    # revoking signs out the browser that used it, and the file stops working
    server.auth.keys.load()          # pick up the last-used time written by the second server
    client.ok("DELETE", "/api/login-keys/" + kid)
    assert other.req("GET", "/api/state").status == 401
    assert other.req("POST", "/login", {"key": key}).status == 403
    assert client.ok("GET", "/api/state")["app"] == "MailTool"       # the code session is unaffected
    assert client.req("DELETE", "/api/login-keys/" + kid).status == 404


def test_login_keys_need_a_session(client):
    assert client.req("POST", "/api/login-keys", {"label": "x"}).status == 401
    assert client.req("GET", "/api/login-keys").status == 401

