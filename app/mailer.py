import logging

from . import config

log = logging.getLogger("mailer")
outbox: list[dict] = []  # used when ACS is not configured (dev/tests)


def send_email(to: str, subject: str, body: str) -> str | None:
    """Returns None on success, or an error description."""
    if not (config.ACS_ENDPOINT and config.MAIL_SENDER):
        outbox.append({"to": to, "subject": subject, "body": body})
        log.warning("ACS not configured; email to %s not sent: %s", to, subject)
        return "ACS not configured"
    try:
        from azure.communication.email import EmailClient
        from azure.identity import DefaultAzureCredential

        client = EmailClient(config.ACS_ENDPOINT, DefaultAzureCredential())
        result = client.begin_send({
            "senderAddress": config.MAIL_SENDER,
            "recipients": {"to": [{"address": to}]},
            "content": {"subject": subject, "plainText": body},
        }).result(timeout=60)
        if result.get("status") != "Succeeded":
            raise RuntimeError(f"status {result.get('status')}: {result.get('error')}")
        return None
    except Exception as e:
        log.exception("Email send failed")
        return str(e)[:300]
