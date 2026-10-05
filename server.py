"""Upsido.ai backend: workshops, registrations and brochure upload.

Dependency-free WSGI app (Python 3.9+, standard library only, SQLite storage).
  Dev:  python server.py
  Prod: gunicorn -w 2 -b 0.0.0.0:8000 server:app     (or waitress-serve server:app)
"""
import base64, csv, hashlib, hmac, io, json, os, re, secrets, sqlite3, sys, threading, time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent


def _load_env():
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@upsido.ai").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
MAX_PDF_BYTES = int(float(os.environ.get("MAX_PDF_MB", "15")) * 1024 * 1024)
TRUST_PROXY = os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "upsido.db"
PDF_PATH = DATA_DIR / "brochure.pdf"
IST = timezone(timedelta(hours=5, minutes=30))

_kf = DATA_DIR / "secret.key"
if not _kf.exists():
    _kf.write_text(secrets.token_hex(32))
SECRET = os.environ.get("SECRET_KEY") or _kf.read_text().strip()


# ---------- helpers ----------
class HTTPError(Exception):
    def __init__(self, status, msg):
        self.status, self.msg = status, msg


class Raw:
    def __init__(self, body, ctype, status=200, headers=None):
        self.body, self.ctype, self.status, self.headers = body, ctype, status, headers or {}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_ist():
    return datetime.now(IST).strftime("%Y-%m-%d")


def db():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS workshops(
      id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, type TEXT, date TEXT NOT NULL,
      start_time TEXT NOT NULL, end_time TEXT NOT NULL, mode TEXT, speaker TEXT, description TEXT,
      learn TEXT, seats INTEGER, price TEXT, status TEXT NOT NULL DEFAULT 'published',
      featured INTEGER NOT NULL DEFAULT 0, created_at TEXT, updated_at TEXT);
    CREATE TABLE IF NOT EXISTS registrations(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      workshop_id INTEGER REFERENCES workshops(id) ON DELETE SET NULL, workshop_title TEXT,
      name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL, whatsapp INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_reg_ws ON registrations(workshop_id);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    """)
    c.commit(); c.close()


def get_setting(c, k, d=None):
    r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    return r["value"] if r else d


def set_setting(c, k, v):
    c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))


# ---------- auth ----------
def _b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def _unb64(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(email):
    body = _b64(json.dumps({"sub": email, "exp": int(time.time()) + 12 * 3600}).encode())
    sig = _b64(hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest())
    return body + "." + sig


def check_token(tok):
    try:
        body, sig = tok.split(".")
        good = _b64(hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        d = json.loads(_unb64(body))
        return d["sub"] if d["exp"] > time.time() else None
    except Exception:
        return None


_hits, _lock = defaultdict(deque), threading.Lock()


def rate_limit(key, limit, window):
    now = time.time()
    with _lock:
        q = _hits[key]
        while q and q[0] < now - window:
            q.popleft()
        if len(q) >= limit:
            raise HTTPError(429, "Too many attempts. Please try again in a few minutes.")
        q.append(now)


# ---------- request / routing ----------
class Req:
    def __init__(s, env):
        s.env, s.method = env, env["REQUEST_METHOD"]
        s.path = env.get("PATH_INFO", "/").rstrip("/") or "/"
        s.q = {k: v[0] for k, v in parse_qs(env.get("QUERY_STRING", ""), keep_blank_values=True).items()}
        fwd = env.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        s.ip = fwd if (TRUST_PROXY and fwd) else env.get("REMOTE_ADDR", "?")

    def header(s, n): return s.env.get("HTTP_" + n.upper().replace("-", "_"), "")

    def body(s, limit):
        n = int(s.env.get("CONTENT_LENGTH") or 0)
        if n > limit:
            raise HTTPError(413, "File or request is too large.")
        return s.env["wsgi.input"].read(n) if n else b""

    def json(s):
        try:
            d = json.loads(s.body(65536) or b"{}")
        except ValueError:
            raise HTTPError(400, "Invalid JSON.")
        if not isinstance(d, dict):
            raise HTTPError(400, "Expected a JSON object.")
        return d


ROUTES = []


def route(method, pattern, admin=False):
    parts = re.split(r"<int:(\w+)>", pattern)  # literal, name, literal, name, ...
    rx = re.compile("^" + "".join(re.escape(p) if i % 2 == 0 else "(?P<%s>\\d+)" % p for i, p in enumerate(parts)) + "$")
    def deco(fn):
        ROUTES.append((method, rx, fn, admin)); return fn
    return deco


def require_admin(req):
    h = req.header("Authorization")
    if not h.lower().startswith("bearer ") or not check_token(h[7:].strip()):
        raise HTTPError(401, "Please sign in again.")


# ---------- validation ----------
EMAIL_RX = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
TIME_RX = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
TYPES = ["Webinar", "Workshop", "Masterclass", "Live Class", "Bootcamp"]
MODES = ["Live online", "In person", "Hybrid"]
STATUSES = ["draft", "published", "hidden"]


def clean_workshop(d, partial=False):
    out = {}
    def has(k): return (not partial) or (k in d)
    def text(k, mx, req=False, mn=0):
        if has(k):
            v = str(d.get(k) or "").strip()
            if (req and len(v) < max(mn, 1)) or len(v) > mx:
                raise HTTPError(422, "%s is required (max %d characters)." % (k.replace("_", " ").title(), mx))
            out[k] = v
    text("title", 150, True, 2); text("speaker", 100); text("description", 1500); text("price", 40)
    if has("type"):
        out["type"] = d.get("type") or "Workshop"
        if out["type"] not in TYPES: raise HTTPError(422, "Type must be one of: " + ", ".join(TYPES))
    if has("mode"):
        out["mode"] = d.get("mode") or "Live online"
        if out["mode"] not in MODES: raise HTTPError(422, "Mode must be one of: " + ", ".join(MODES))
    if has("status"):
        out["status"] = d.get("status") or "published"
        if out["status"] not in STATUSES: raise HTTPError(422, "Status must be one of: " + ", ".join(STATUSES))
    if has("date"):
        try: datetime.strptime(str(d.get("date")), "%Y-%m-%d")
        except ValueError: raise HTTPError(422, "Date must look like 2026-11-30.")
        out["date"] = str(d["date"])
    for k in ("start_time", "end_time"):
        if has(k):
            if not TIME_RX.match(str(d.get(k) or "")): raise HTTPError(422, "%s must look like 19:00." % k.replace("_", " ").title())
            out[k] = d[k]
    if out.get("start_time") and out.get("end_time") and out["end_time"] <= out["start_time"]:
        raise HTTPError(422, "End time must be after start time.")
    if has("seats"):
        s = d.get("seats")
        if s in (None, ""): out["seats"] = None
        else:
            try: s = int(s)
            except (TypeError, ValueError): raise HTTPError(422, "Seats must be a whole number.")
            if not 0 <= s <= 100000: raise HTTPError(422, "Seats must be between 0 and 100000.")
            out["seats"] = s
    if has("learn"):
        l = d.get("learn") or []
        if isinstance(l, str): l = l.splitlines()
        l = [str(x).strip() for x in l if str(x).strip()]
        if len(l) > 10 or any(len(x) > 200 for x in l): raise HTTPError(422, "Up to 10 learning points, 200 characters each.")
        out["learn"] = json.dumps(l)
    if "featured" in d: out["featured"] = 1 if d["featured"] else 0
    return out


def ws_dict(r, c=None, admin=False):
    d = {k: r[k] for k in ("id", "title", "type", "date", "start_time", "end_time", "mode", "speaker",
                          "description", "price", "seats", "status")}
    d["learn"] = json.loads(r["learn"] or "[]"); d["featured"] = bool(r["featured"])
    if c is not None:
        n = c.execute("SELECT COUNT(*) FROM registrations WHERE workshop_id=?", (r["id"],)).fetchone()[0]
        d["registrations"] = n
        d["seats_left"] = None if r["seats"] is None else max(r["seats"] - n, 0)
    return d


# ---------- public routes ----------
@route("GET", "/api/health")
def health(req): return {"status": "ok", "time": now_iso()}


@route("GET", "/api/workshops")
def public_workshops(req):
    c = db()
    rows = c.execute("SELECT * FROM workshops WHERE status='published' AND date>=? "
                     "ORDER BY featured DESC, date, start_time", (today_ist(),)).fetchall()
    out = [ws_dict(r, c) for r in rows]; c.close()
    for w in out: w.pop("status"); w.pop("registrations")
    return out


@route("POST", "/api/registrations")
def register(req):
    rate_limit("reg:" + req.ip, 10, 600)
    d = req.json()
    name, email = str(d.get("name", "")).strip(), str(d.get("email", "")).strip().lower()
    phone = str(d.get("phone", "")).strip()
    if not 2 <= len(name) <= 100: raise HTTPError(422, "Please enter your name.")
    if not EMAIL_RX.match(email) or len(email) > 160: raise HTTPError(422, "Please enter a valid email.")
    digits = re.sub(r"\D", "", phone)
    if not 8 <= len(digits) <= 15 or not re.match(r"^[0-9+()\-\s]{8,25}$", phone):
        raise HTTPError(422, "Please enter a valid contact number.")
    c = db(); wid, title = d.get("workshop_id"), "General interest"
    try:
        if wid not in (None, ""):
            try: wid = int(wid)
            except (TypeError, ValueError): raise HTTPError(422, "Invalid workshop.")
            w = c.execute("SELECT * FROM workshops WHERE id=? AND status='published'", (wid,)).fetchone()
            if not w: raise HTTPError(404, "This workshop is no longer open for registration.")
            title = w["title"]
            if w["seats"] is not None:
                n = c.execute("SELECT COUNT(*) FROM registrations WHERE workshop_id=?", (wid,)).fetchone()[0]
                if n >= w["seats"]: raise HTTPError(409, "Sorry, this workshop is full.")
        else:
            wid = None
        dup = c.execute("SELECT id FROM registrations WHERE email=? AND workshop_id IS ?", (email, wid)).fetchone()
        if dup: return {"ok": True, "already_registered": True}
        c.execute("INSERT INTO registrations(workshop_id,workshop_title,name,email,phone,whatsapp,created_at) VALUES(?,?,?,?,?,?,?)",
                  (wid, title, name, email, phone, 1 if d.get("whatsapp") else 0, now_iso()))
        c.commit()
    finally:
        c.close()
    return (201, {"ok": True})


@route("GET", "/api/brochure/info")
def brochure_info(req):
    c = db(); name = get_setting(c, "brochure_name"); upd = get_setting(c, "brochure_updated"); c.close()
    if not PDF_PATH.exists(): return {"available": False}
    return {"available": True, "name": name, "size": PDF_PATH.stat().st_size, "updated_at": upd}


@route("GET", "/api/brochure")
def brochure(req):
    if not PDF_PATH.exists(): raise HTTPError(404, "No brochure has been uploaded yet.")
    c = db(); name = get_setting(c, "brochure_name") or "Upsido-Brochure.pdf"; c.close()
    return Raw(PDF_PATH.read_bytes(), "application/pdf", headers={
        "Content-Disposition": 'attachment; filename="%s"' % re.sub(r'[^\w.\- ]', "_", name), "Cache-Control": "no-cache"})


# ---------- admin routes ----------
@route("POST", "/api/admin/login")
def login(req):
    rate_limit("login:" + req.ip, 8, 60)
    d = req.json()
    ok = bool(ADMIN_PASSWORD) and hmac.compare_digest(str(d.get("email", "")).strip().lower(), ADMIN_EMAIL) \
        and hmac.compare_digest(str(d.get("password", "")), ADMIN_PASSWORD)
    if not ok: raise HTTPError(401, "Invalid email or password.")
    return {"token": make_token(ADMIN_EMAIL), "email": ADMIN_EMAIL}


@route("GET", "/api/admin/stats", True)
def stats(req):
    c = db(); q = lambda s, *a: c.execute(s, a).fetchone()[0]
    out = {"workshops": q("SELECT COUNT(*) FROM workshops"), "published": q("SELECT COUNT(*) FROM workshops WHERE status='published'"),
           "registrations": q("SELECT COUNT(*) FROM registrations"), "brochure": PDF_PATH.exists()}
    c.close(); return out


@route("GET", "/api/admin/workshops", True)
def admin_workshops(req):
    c = db(); rows = c.execute("SELECT * FROM workshops ORDER BY date DESC, start_time DESC").fetchall()
    out = [ws_dict(r, c) for r in rows]; c.close(); return out


@route("POST", "/api/admin/workshops", True)
def create_workshop(req):
    f = clean_workshop(req.json())
    f.setdefault("type", "Workshop"); f.setdefault("mode", "Live online"); f.setdefault("status", "published")
    f.setdefault("learn", "[]"); f.setdefault("featured", 0); f["created_at"] = f["updated_at"] = now_iso()
    c = db(); cur = c.execute("INSERT INTO workshops(%s) VALUES(%s)" % (",".join(f), ",".join("?" * len(f))), list(f.values()))
    c.commit(); r = c.execute("SELECT * FROM workshops WHERE id=?", (cur.lastrowid,)).fetchone()
    out = ws_dict(r, c); c.close(); return (201, out)


@route("PATCH", "/api/admin/workshops/<int:id>", True)
def update_workshop(req, id):
    f = clean_workshop(req.json(), partial=True)
    if not f: raise HTTPError(400, "Nothing to update.")
    f["updated_at"] = now_iso(); c = db()
    cur = c.execute("UPDATE workshops SET %s WHERE id=?" % ",".join(k + "=?" for k in f), list(f.values()) + [id])
    if cur.rowcount == 0: c.close(); raise HTTPError(404, "Workshop not found.")
    c.commit(); out = ws_dict(c.execute("SELECT * FROM workshops WHERE id=?", (id,)).fetchone(), c); c.close(); return out


@route("DELETE", "/api/admin/workshops/<int:id>", True)
def delete_workshop(req, id):
    c = db(); cur = c.execute("DELETE FROM workshops WHERE id=?", (id,)); c.commit(); c.close()
    if cur.rowcount == 0: raise HTTPError(404, "Workshop not found.")
    return {"ok": True}


def _reg_query(req):
    sql, a = "SELECT * FROM registrations WHERE 1=1", []
    if req.q.get("workshop_id", "").isdigit(): sql += " AND workshop_id=?"; a.append(int(req.q["workshop_id"]))
    if req.q.get("workshop_id") == "none": sql += " AND workshop_id IS NULL"
    qs = req.q.get("q", "").strip().lower()
    if qs:
        sql += " AND (lower(name) LIKE ? OR lower(email) LIKE ? OR phone LIKE ?)"; a += ["%" + qs + "%"] * 3
    return sql + " ORDER BY created_at DESC LIMIT 5000", a


@route("GET", "/api/admin/registrations", True)
def admin_regs(req):
    c = db(); sql, a = _reg_query(req)
    out = [dict(r, whatsapp=bool(r["whatsapp"])) for r in c.execute(sql, a).fetchall()]; c.close(); return out


@route("GET", "/api/admin/registrations.csv", True)
def regs_csv(req):
    c = db(); sql, a = _reg_query(req); rows = c.execute(sql, a).fetchall(); c.close()
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["Name", "Email", "Phone", "WhatsApp", "Workshop", "Registered at (UTC)"])
    safe = lambda v: ("'" + v) if v[:1] in ("=", "+", "-", "@") else v  # block spreadsheet formula injection
    for r in rows:
        w.writerow([safe(r["name"]), safe(r["email"]), safe(r["phone"]), "Yes" if r["whatsapp"] else "No", safe(r["workshop_title"] or ""), r["created_at"]])
    return Raw(buf.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
               headers={"Content-Disposition": 'attachment; filename="upsido-registrations.csv"'})


@route("DELETE", "/api/admin/registrations/<int:id>", True)
def delete_reg(req, id):
    c = db(); cur = c.execute("DELETE FROM registrations WHERE id=?", (id,)); c.commit(); c.close()
    if cur.rowcount == 0: raise HTTPError(404, "Registration not found.")
    return {"ok": True}


@route("POST", "/api/admin/brochure", True)
def upload_brochure(req):
    data = req.body(MAX_PDF_BYTES)
    if not data.startswith(b"%PDF-"): raise HTTPError(415, "Only PDF files are accepted.")
    name = re.sub(r"[^\w.\- ]", "_", os.path.basename(req.header("X-Filename") or "brochure.pdf"))[:100]
    if not name.lower().endswith(".pdf"): name += ".pdf"
    tmp = PDF_PATH.with_suffix(".tmp"); tmp.write_bytes(data); os.replace(tmp, PDF_PATH)
    c = db(); set_setting(c, "brochure_name", name); set_setting(c, "brochure_updated", now_iso()); c.commit(); c.close()
    return {"ok": True, "name": name, "size": len(data)}


@route("DELETE", "/api/admin/brochure", True)
def delete_brochure(req):
    if PDF_PATH.exists(): PDF_PATH.unlink()
    return {"ok": True}


@route("GET", "/admin")
def admin_page(req):
    return Raw((ROOT / "static" / "admin.html").read_bytes(), "text/html; charset=utf-8", headers={
        "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'",
        "Cache-Control": "no-store"})


@route("GET", "/")
def index(req): return {"service": "upsido.ai backend", "admin": "/admin"}


# ---------- WSGI entry ----------
STATUS = {200: "OK", 201: "Created", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
          405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large", 415: "Unsupported Media Type",
          422: "Unprocessable Entity", 429: "Too Many Requests", 500: "Internal Server Error"}


def app(env, start):
    req, origin = Req(env), env.get("HTTP_ORIGIN", "")
    h = [("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"), ("Vary", "Origin")]
    if "*" in CORS_ORIGINS: h.append(("Access-Control-Allow-Origin", "*"))
    elif origin in CORS_ORIGINS: h.append(("Access-Control-Allow-Origin", origin))
    h += [("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Filename"),
          ("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS"), ("Access-Control-Expose-Headers", "Content-Disposition")]
    try:
        if req.method == "OPTIONS":
            start("204 No Content", h); return [b""]
        found, path_ok = None, False
        for m, rx, fn, adm in ROUTES:
            mt = rx.match(req.path)
            if mt:
                path_ok = True
                if m == req.method: found = (fn, mt.groupdict(), adm); break
        if not found: raise HTTPError(405 if path_ok else 404, "Method not allowed." if path_ok else "Not found.")
        fn, params, adm = found
        if adm: require_admin(req)
        res = fn(req, **{k: int(v) for k, v in params.items()})
        status = 200
        if isinstance(res, tuple): status, res = res
        if isinstance(res, Raw):
            body, ctype, status, extra = res.body, res.ctype, res.status, list(res.headers.items())
        else:
            body, ctype, extra = json.dumps(res).encode(), "application/json", []
    except HTTPError as e:
        status, body, ctype, extra = e.status, json.dumps({"error": e.msg}).encode(), "application/json", []
    except Exception as e:  # never leak internals
        print("ERROR", req.method, req.path, repr(e), file=sys.stderr)
        status, body, ctype, extra = 500, b'{"error":"Something went wrong."}', "application/json", []
    start("%d %s" % (status, STATUS.get(status, "OK")), h + [("Content-Type", ctype), ("Content-Length", str(len(body)))] + extra)
    return [body]


init_db()
if not ADMIN_PASSWORD:
    print("WARNING: ADMIN_PASSWORD is not set, so admin login is disabled. Copy .env.example to .env and set it.", file=sys.stderr)

if __name__ == "__main__":
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

    class Threaded(ThreadingMixIn, WSGIServer): daemon_threads = True
    class Quiet(WSGIRequestHandler):
        def log_message(self, *a): pass
    port = int(os.environ.get("PORT", "8000"))
    print("Upsido backend running on http://0.0.0.0:%d  (admin: /admin)" % port)
    make_server("0.0.0.0", port, app, server_class=Threaded, handler_class=Quiet).serve_forever()
