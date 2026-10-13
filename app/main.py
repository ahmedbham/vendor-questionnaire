import hmac
import secrets
import time
from pathlib import Path

import pyotp
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import config
from .excel import export_responses, import_questions
from .mailer import send_email
from .security import new_otp, new_token, verify_password
from .store import make_repo

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=config.SESSION_SECRET, https_only=config.HTTPS_ONLY,
                   same_site="strict", max_age=config.SESSION_MAX_AGE)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
repo = make_repo()
_admin_fails: dict[str, list[float]] = {}
MAX_UPLOAD = 2 * 1024 * 1024


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'"
    return resp


def csrf(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(24)
    return request.session["csrf"]


def check_csrf(request: Request, token: str) -> None:
    if not hmac.compare_digest(request.session.get("csrf", ""), token or ""):
        raise HTTPException(403, "Invalid CSRF token")


def render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, {"csrf": csrf(request), **ctx})


def current_vendor(request: Request) -> dict:
    """The only source of vendor identity: the signed session, never a request parameter."""
    vid = request.session.get("vendor_id")
    vendor = repo.get_vendor(vid) if vid else None
    if not vendor:
        raise HTTPException(401, "Not signed in")
    return vendor


def require_admin(request: Request) -> None:
    if not request.session.get("admin"):
        raise HTTPException(401, "Admin sign-in required")


@app.exception_handler(401)
async def unauthorized(request: Request, exc):
    if request.url.path.startswith("/admin"):
        return RedirectResponse("/admin/login", 303)
    return templates.TemplateResponse(
        request, "message.html", {"csrf": csrf(request), "message": "Please use the invitation link from your email."},
        status_code=401)


@app.get("/")
def home(request: Request):
    return render(request, "message.html", message="Contoso Health vendor questionnaire. Use the link in your invitation email.")


# ---------- vendor auth ----------
@app.get("/invite/{token}")
def invite(request: Request, token: str):
    vid = repo.redeem_invite(token)
    if not vid:
        return render(request, "message.html", message="This invitation link is invalid or expired.")
    request.session.clear()
    request.session["pending_vendor"] = vid
    return render(request, "send_code.html")


@app.post("/send-code")
def send_code(request: Request, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    vid = request.session.get("pending_vendor")
    vendor = repo.get_vendor(vid) if vid else None
    if not vendor:
        raise HTTPException(401)
    if time.time() - request.session.get("last_code", 0) < 60:
        return render(request, "verify.html", error="Please wait a minute before requesting another code.")
    request.session["last_code"] = time.time()
    otp = new_otp()
    repo.set_otp(vid, otp)
    err = send_email(vendor["email"], "Your Contoso Health questionnaire code",
                     f"Your one-time code is {otp}. It expires in {config.OTP_TTL_MINUTES} minutes.")
    repo.audit(vid, "otp_sent" if not err else "otp_email_failed", err or "")
    return render(request, "verify.html", error=None,
                  debug=f"TEST MODE – code: {otp} (email error: {err})" if config.DEBUG_SHOW_OTP else None)


@app.post("/verify")
def verify(request: Request, code: str = Form(""), csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    vid = request.session.get("pending_vendor")
    if not vid:
        raise HTTPException(401)
    if not repo.check_otp(vid, code):
        repo.audit(vid, "otp_failed")
        return render(request, "verify.html", error="Invalid or expired code.")
    request.session.clear()
    request.session["vendor_id"] = vid
    repo.audit(vid, "login")
    return RedirectResponse("/questionnaire", 303)


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/", 303)


# ---------- vendor questionnaire ----------
@app.get("/questionnaire", response_class=HTMLResponse)
def questionnaire(request: Request):
    vendor = current_vendor(request)
    resp = repo.get_response(vendor["id"])
    return render(request, "questionnaire.html", vendor=vendor, questions=repo.get_questions(),
                  answers=resp["answers"], status=resp["status"])


@app.post("/questionnaire")
async def save_questionnaire(request: Request):
    vendor = current_vendor(request)
    form = await request.form()
    check_csrf(request, form.get("csrf_token", ""))
    answers = {k[2:]: v for k, v in form.items() if k.startswith("a_")}
    repo.save_response(vendor["id"], answers, submit=form.get("action") == "submit")
    return RedirectResponse("/questionnaire", 303)


# ---------- admin ----------
@app.get("/admin/login")
def admin_login_page(request: Request):
    return render(request, "admin_login.html", error=None)


@app.post("/admin/login")
def admin_login(request: Request, password: str = Form(""), totp: str = Form(""), csrf_token: str = Form("")):
    check_csrf(request, csrf_token)
    ip = request.client.host if request.client else "?"
    fails = [t for t in _admin_fails.get(ip, []) if time.time() - t < 900]
    if len(fails) >= 5:
        return render(request, "admin_login.html", error="Too many attempts. Try again later.")
    ok = bool(config.ADMIN_PASSWORD_HASH) and verify_password(password, config.ADMIN_PASSWORD_HASH)
    if ok and config.ADMIN_TOTP_SECRET:
        ok = pyotp.TOTP(config.ADMIN_TOTP_SECRET).verify(totp, valid_window=1)
    if not ok:
        _admin_fails[ip] = fails + [time.time()]
        return render(request, "admin_login.html", error="Invalid credentials.")
    request.session.clear()
    request.session["admin"] = True
    return RedirectResponse("/admin", 303)


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    require_admin(request)
    return render(request, "admin.html", vendors=repo.list_vendors(),
                  question_count=len(repo.get_questions()), invite_link=request.session.pop("invite_link", ""))


@app.post("/admin/vendors")
def admin_create_vendor(request: Request, name: str = Form(...), email: str = Form(...), csrf_token: str = Form("")):
    require_admin(request)
    check_csrf(request, csrf_token)
    vendor = repo.create_vendor(name.strip(), email)
    return _issue_invite(request, vendor)


@app.post("/admin/vendors/{vendor_id}/invite")
def admin_reinvite(request: Request, vendor_id: str, csrf_token: str = Form("")):
    require_admin(request)
    check_csrf(request, csrf_token)
    vendor = repo.get_vendor(vendor_id)
    if not vendor:
        raise HTTPException(404)
    return _issue_invite(request, vendor)


def _issue_invite(request: Request, vendor: dict):
    token = new_token()
    repo.create_invite(vendor["id"], token)
    link = f"{config.BASE_URL}/invite/{token}"
    err = send_email(vendor["email"], "Contoso Health IT questionnaire",
                     f"Please complete the questionnaire using this link: {link}")
    request.session["invite_link"] = link + (f"  (email failed: {err})" if err else "")
    return RedirectResponse("/admin", 303)


@app.post("/admin/questions")
async def admin_import(request: Request, file: UploadFile = File(...), csrf_token: str = Form("")):
    require_admin(request)
    check_csrf(request, csrf_token)
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "Upload an .xlsx file under 2 MB")
    try:
        questions = import_questions(data)
    except Exception:
        raise HTTPException(400, "Could not read the Excel file")
    repo.set_questions(questions)
    return RedirectResponse("/admin", 303)


@app.get("/admin/vendors/{vendor_id}", response_class=HTMLResponse)
def admin_vendor(request: Request, vendor_id: str):
    require_admin(request)
    vendor = repo.get_vendor(vendor_id)
    if not vendor:
        raise HTTPException(404)
    resp = repo.get_response(vendor_id)
    return render(request, "admin_vendor.html", vendor=vendor, questions=repo.get_questions(),
                  answers=resp["answers"], status=resp["status"], audit=repo.get_audit(vendor_id))


@app.get("/admin/vendors/{vendor_id}/export")
def admin_export(request: Request, vendor_id: str):
    require_admin(request)
    vendor = repo.get_vendor(vendor_id)
    if not vendor:
        raise HTTPException(404)
    data = export_responses(vendor, repo.get_questions(), repo.get_response(vendor_id))
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{vendor_id}.xlsx"'})
