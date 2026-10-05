"""Run: python -m unittest tests.test_api -v   (uses a temporary database)"""
import os, sys, tempfile, threading, unittest
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.update(DATA_DIR=tempfile.mkdtemp(), ADMIN_EMAIL="admin@test.io", ADMIN_PASSWORD="s3cret-password", MAX_PDF_MB="1", CORS_ORIGINS="https://upsido.ai")
import requests
import server
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server


class T(ThreadingMixIn, WSGIServer): daemon_threads = True
class Q(WSGIRequestHandler):
    def log_message(self, *a): pass


def future(days): return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
WS = dict(title="AI for Excel", type="Workshop", date=future(10), start_time="19:00", end_time="20:30", mode="Live online",
          speaker="Raunak Singh", description="Hands-on session.", learn=["Clean data", "Build a report"], seats=2, price="Free")


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = make_server("127.0.0.1", 0, server.app, server_class=T, handler_class=Q)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        r = requests.post(cls.base + "/api/admin/login", json={"email": "admin@test.io", "password": "s3cret-password"})
        cls.h = {"Authorization": "Bearer " + r.json()["token"]}

    @classmethod
    def tearDownClass(cls): cls.srv.shutdown()

    def u(self, p): return self.base + p

    def test_01_health_and_cors(self):
        self.assertEqual(requests.get(self.u("/api/health")).json()["status"], "ok")
        r = requests.options(self.u("/api/registrations"), headers={"Origin": "https://upsido.ai"})
        self.assertEqual(r.status_code, 204); self.assertEqual(r.headers["Access-Control-Allow-Origin"], "https://upsido.ai")
        r = requests.get(self.u("/api/health"), headers={"Origin": "https://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)

    def test_02_auth(self):
        self.assertEqual(requests.get(self.u("/api/admin/workshops")).status_code, 401)
        self.assertEqual(requests.get(self.u("/api/admin/workshops"), headers={"Authorization": "Bearer x.y"}).status_code, 401)
        self.assertEqual(requests.post(self.u("/api/admin/login"), json={"email": "admin@test.io", "password": "bad"}).status_code, 401)
        self.assertEqual(requests.get(self.u("/admin")).status_code, 200)

    def test_03_workshop_lifecycle(self):
        r = requests.post(self.u("/api/admin/workshops"), json=WS, headers=self.h); self.assertEqual(r.status_code, 201, r.text)
        wid = r.json()["id"]; self.assertEqual(r.json()["learn"], ["Clean data", "Build a report"])
        pub = requests.get(self.u("/api/workshops")).json(); self.assertIn(wid, [w["id"] for w in pub])
        self.assertNotIn("status", pub[0]); self.assertNotIn("registrations", pub[0])
        requests.patch(self.u("/api/admin/workshops/%d" % wid), json={"status": "draft"}, headers=self.h)
        self.assertNotIn(wid, [w["id"] for w in requests.get(self.u("/api/workshops")).json()])
        requests.patch(self.u("/api/admin/workshops/%d" % wid), json={"status": "published", "featured": True}, headers=self.h)
        self.assertTrue(requests.get(self.u("/api/workshops")).json()[0]["featured"])
        past = dict(WS, title="Old one", date=future(-3))
        pid = requests.post(self.u("/api/admin/workshops"), json=past, headers=self.h).json()["id"]
        self.assertNotIn(pid, [w["id"] for w in requests.get(self.u("/api/workshops")).json()])
        for bad in ({"title": ""}, {"date": "31-12-2026"}, {"start_time": "25:00"}, {"mode": "Moon"}, {"seats": "many"}):
            self.assertEqual(requests.post(self.u("/api/admin/workshops"), json=dict(WS, **bad), headers=self.h).status_code, 422, bad)
        self.assertEqual(requests.patch(self.u("/api/admin/workshops/%d" % wid), json={}, headers=self.h).status_code, 400)
        self.assertEqual(requests.patch(self.u("/api/admin/workshops/9999"), json={"title": "Zed"}, headers=self.h).status_code, 404)
        for i in (wid, pid): self.assertEqual(requests.delete(self.u("/api/admin/workshops/%d" % i), headers=self.h).status_code, 200)
        self.assertEqual(requests.delete(self.u("/api/admin/workshops/%d" % wid), headers=self.h).status_code, 404)

    def test_04_registrations(self):
        server._hits.clear()
        wid = requests.post(self.u("/api/admin/workshops"), json=WS, headers=self.h).json()["id"]
        reg = lambda **k: requests.post(self.u("/api/registrations"), json=dict({"name": "Asha Rao", "email": "asha@example.com", "phone": "+91 98765 43210", "whatsapp": True, "workshop_id": wid}, **k))
        self.assertEqual(reg().status_code, 201)
        r = reg(); self.assertEqual(r.status_code, 200); self.assertTrue(r.json()["already_registered"])
        for bad in ({"name": "A"}, {"email": "nope"}, {"phone": "12"}, {"phone": "abcdefghij"}, {"workshop_id": 9999}):
            self.assertIn(reg(**bad).status_code, (404, 422), bad)
        self.assertEqual(reg(email="b@example.com").status_code, 201)
        self.assertEqual(reg(email="c@example.com").status_code, 409)  # seats=2 -> full
        self.assertEqual(requests.get(self.u("/api/workshops")).json()[0]["seats_left"], 0)
        self.assertEqual(requests.post(self.u("/api/registrations"), json={"name": "Gen Eral", "email": "g@example.com", "phone": "9876543210"}).status_code, 201)
        rows = requests.get(self.u("/api/admin/registrations"), params={"workshop_id": wid}, headers=self.h).json()
        self.assertEqual(len(rows), 2); self.assertTrue(rows[0]["whatsapp"])
        self.assertEqual(len(requests.get(self.u("/api/admin/registrations"), params={"q": "asha@"}, headers=self.h).json()), 1)
        csvr = requests.get(self.u("/api/admin/registrations.csv"), headers=self.h); self.assertIn("text/csv", csvr.headers["Content-Type"])
        self.assertIn("asha@example.com", csvr.text)
        server._hits.clear()  # registration endpoint is rate limited (10 per 10 min per IP)
        requests.post(self.u("/api/registrations"), json={"name": "=HYPERLINK(1)", "email": "f@example.com", "phone": "9876543210"})
        self.assertIn("'=HYPERLINK", requests.get(self.u("/api/admin/registrations.csv"), headers=self.h).text)
        self.assertEqual(requests.get(self.u("/api/admin/registrations")).status_code, 401)
        rid = rows[0]["id"]; self.assertEqual(requests.delete(self.u("/api/admin/registrations/%d" % rid), headers=self.h).status_code, 200)
        self.assertEqual(requests.delete(self.u("/api/admin/registrations/%d" % rid), headers=self.h).status_code, 404)
        requests.delete(self.u("/api/admin/workshops/%d" % wid), headers=self.h)

    def test_05_brochure(self):
        self.assertFalse(requests.get(self.u("/api/brochure/info")).json()["available"])
        self.assertEqual(requests.get(self.u("/api/brochure")).status_code, 404)
        pdf = b"%PDF-1.4\n" + b"x" * 5000
        up = lambda data, name="Upsido Brochure.pdf", h=None: requests.post(self.u("/api/admin/brochure"), data=data, headers=dict(h or self.h, **{"X-Filename": name, "Content-Type": "application/pdf"}))
        self.assertEqual(up(pdf, h={"X-Test": "1"}).status_code, 401)
        self.assertEqual(up(b"<html>not a pdf</html>").status_code, 415)
        self.assertEqual(up(b"%PDF-1.4" + b"x" * (1024 * 1024 + 10)).status_code, 413)
        self.assertEqual(up(pdf).status_code, 200)
        info = requests.get(self.u("/api/brochure/info")).json(); self.assertTrue(info["available"]); self.assertEqual(info["size"], len(pdf))
        r = requests.get(self.u("/api/brochure")); self.assertEqual(r.content, pdf); self.assertIn("attachment", r.headers["Content-Disposition"])
        up(pdf + b"v2"); self.assertEqual(requests.get(self.u("/api/brochure")).content, pdf + b"v2")
        self.assertEqual(requests.delete(self.u("/api/admin/brochure"), headers=self.h).status_code, 200)
        self.assertEqual(requests.get(self.u("/api/brochure")).status_code, 404)

    def test_06_rate_limits(self):
        server._hits.clear()
        codes = [requests.post(self.u("/api/admin/login"), json={"email": "admin@test.io", "password": "bad%d" % i}).status_code for i in range(10)]
        self.assertEqual(codes[:8], [401] * 8); self.assertEqual(codes[-1], 429)
        server._hits.clear()

    def test_07_unknown(self):
        self.assertEqual(requests.get(self.u("/api/nope")).status_code, 404)
        self.assertEqual(requests.delete(self.u("/api/workshops")).status_code, 405)


if __name__ == "__main__": unittest.main()
