import io
import os
import re

os.environ["ADMIN_PASSWORD_HASH"] = __import__("app.security", fromlist=["x"]).hash_password("adminpw", iterations=1000)

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app import mailer
from app.main import app, repo


def token(client, path="/admin/login"):
    return re.search(r'name="csrf_token" value="([^"]+)"', client.get(path).text).group(1)


def xlsx():
    wb = Workbook()
    wb.active.append(["Section", "Question", "Type"])
    wb.active.append(["Security", "Do you encrypt data at rest?", "yesno"])
    wb.active.append(["Governance", "Describe your incident process", "longtext"])
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


@pytest.fixture(scope="module")
def setup():
    admin = TestClient(app)
    r = admin.post("/admin/login", data={"password": "adminpw", "csrf_token": token(admin)}, follow_redirects=False)
    assert r.status_code == 303
    t = token(admin, "/admin")
    admin.post("/admin/questions", data={"csrf_token": t}, files={"file": ("q.xlsx", xlsx())})
    links = {}
    for name in ("Acme", "Globex"):
        mailer.outbox.clear()
        admin.post("/admin/vendors", data={"name": name, "email": f"{name}@x.com", "csrf_token": t})
        links[name] = re.search(r"http\S+/invite/\S+", mailer.outbox[-1]["body"]).group(0)
    return admin, links


def login(link):
    c = TestClient(app)
    path = "/invite/" + link.rsplit("/", 1)[1]
    c.get(path)
    t = token(c, path)
    mailer.outbox.clear()
    c.post("/send-code", data={"csrf_token": t})
    otp = re.search(r"code is (\d{6})", mailer.outbox[-1]["body"]).group(1)
    r = c.post("/verify", data={"code": otp, "csrf_token": t}, follow_redirects=False)
    assert r.status_code == 303
    return c


def test_vendors_only_see_own_data(setup):
    _, links = setup
    a, b = login(links["Acme"]), login(links["Globex"])
    ta = token(a, "/questionnaire")
    a.post("/questionnaire", data={"a_q1": "Yes", "a_q2": "ACME-SECRET", "action": "save", "csrf_token": ta})
    page_a, page_b = a.get("/questionnaire").text, b.get("/questionnaire").text
    assert "ACME-SECRET" in page_a and "Acme" in page_a
    assert "ACME-SECRET" not in page_b and "Globex" in page_b and "Acme" not in page_b


def test_vendor_cannot_reach_admin_or_others(setup):
    admin, links = setup
    a = login(links["Acme"])
    vid = repo.list_vendors()[1]["id"]
    for p in ("/admin", f"/admin/vendors/{vid}", f"/admin/vendors/{vid}/export"):
        assert a.get(p, follow_redirects=False).status_code == 303  # bounced to admin login
    assert TestClient(app).get("/questionnaire").status_code == 401


def test_submitted_is_locked_and_admin_sees_it(setup):
    admin, links = setup
    c = login(links["Globex"])
    t = token(c, "/questionnaire")
    c.post("/questionnaire", data={"a_q1": "No", "action": "submit", "csrf_token": t})
    c.post("/questionnaire", data={"a_q1": "Yes", "action": "save", "csrf_token": t})
    vid = next(v["id"] for v in repo.list_vendors() if v["name"] == "Globex")
    assert repo.get_response(vid)["answers"]["q1"] == "No"
    assert admin.get(f"/admin/vendors/{vid}/export").status_code == 200


def test_otp_lockout_and_csrf(setup):
    _, links = setup
    c = TestClient(app)
    path = "/invite/" + links["Acme"].rsplit("/", 1)[1]
    c.get(path); t = token(c, path)
    mailer.outbox.clear()
    c.post("/send-code", data={"csrf_token": t})
    otp = re.search(r"code is (\d{6})", mailer.outbox[-1]["body"]).group(1)
    for _ in range(5):
        c.post("/verify", data={"code": "000000" if otp != "000000" else "111111", "csrf_token": t})
    r = c.post("/verify", data={"code": otp, "csrf_token": t}, follow_redirects=False)
    assert r.status_code == 200  # locked out even with the right code
    assert c.post("/verify", data={"code": otp, "csrf_token": "bad"}).status_code == 403


def test_bad_invite(setup):
    assert "invalid" in TestClient(app).get("/invite/nope").text
