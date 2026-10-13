import os

SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-only-change-me")
# Format: "pbkdf2$<iterations>$<salt hex>$<hash hex>" (see security.hash_password)
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")
ADMIN_TOTP_SECRET = os.environ.get("ADMIN_TOTP_SECRET", "")
# Testing only: show the OTP on the verify page and the email error on the admin page.
DEBUG_SHOW_OTP = os.environ.get("DEBUG_SHOW_OTP", "false").lower() == "true"
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
COSMOS_ENDPOINT = os.environ.get("COSMOS_ENDPOINT", "")
COSMOS_DATABASE = os.environ.get("COSMOS_DATABASE", "questionnaire")
ACS_ENDPOINT = os.environ.get("ACS_ENDPOINT", "")
MAIL_SENDER = os.environ.get("MAIL_SENDER", "")
HTTPS_ONLY = os.environ.get("HTTPS_ONLY", "false").lower() == "true"
INVITE_TTL_HOURS = 72
OTP_TTL_MINUTES = 10
MAX_OTP_ATTEMPTS = 5
SESSION_MAX_AGE = 60 * 60 * 4
