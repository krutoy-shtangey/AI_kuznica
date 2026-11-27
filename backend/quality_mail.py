"""Configured SMTP delivery with a durable, bounded outbox."""

import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from time import time
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import certifi


class MailUncertain(Exception):
    """The relay may have accepted the message. Do not send it again."""


class MailFailed(Exception):
    """The relay definitively rejected the message."""


class MailRetry(Exception):
    """The message was not accepted and may be retried."""


def _address(value):
    if not re.fullmatch(r"[^\s@,<>]+@[^\s@,<>]+\.[^\s@,<>]+", value):
        raise ValueError("SMTP_FROM and QUALITY_EMAIL must each be one email address")
    return value


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    username: str | None
    password: str | None = field(repr=False)
    sender: str
    recipient: str
    tls: bool
    ssl: bool
    public_url: str | None

    @classmethod
    def load(cls, values):
        def get(key, default=""):
            return (values.get(key) or default).strip()

        def boolean(key, default=False):
            value = get(key, "1" if default else "0").lower()
            if value not in {"1", "0", "true", "false"}:
                raise ValueError(f"Invalid boolean for {key}")
            return value in {"1", "true"}

        if not boolean("SMTP_ENABLED"):
            return None
        use_ssl = boolean("SMTP_USE_SSL")
        tls = boolean("SMTP_USE_TLS", not use_ssl)
        username, password = get("SMTP_USERNAME"), get("SMTP_PASSWORD")
        host, port = get("SMTP_HOST"), int(get("SMTP_PORT", "465" if use_ssl else "587"))
        if not host or any(char.isspace() for char in host) or not 1 <= port <= 65535 or (tls and use_ssl):
            raise ValueError("Invalid SMTP server configuration")
        if bool(username) != bool(password) or username and not (tls or use_ssl):
            raise ValueError("SMTP credentials require TLS or SSL")
        public = get("PUBLIC_APP_URL").rstrip("/") or None
        if public:
            url = urlsplit(public)
            if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.query or url.fragment:
                raise ValueError("Invalid PUBLIC_APP_URL")
        return cls(host, port, username or None, password or None, _address(get("SMTP_FROM")),
                   _address(get("QUALITY_EMAIL")), tls, use_ssl, public)


class SMTPMailer:
    def __init__(self, settings: SMTPSettings):
        self.settings = settings

    def send(self, appeal):
        config = self.settings
        message = EmailMessage()
        message["From"], message["To"] = config.sender, config.recipient
        message["Subject"] = f"ИИКузница — обращение {appeal['id']}"
        message["Message-ID"] = f"<quality-{appeal['id']}@{config.sender.rsplit('@', 1)[-1]}>"
        labels = {"NO_ANSWER": "В найденных источниках недостаточно сведений",
                  "CONFLICT": "В найденных источниках есть противоречие"}
        text = [
            f"Номер обращения: {appeal['id']}", f"Вопрос: {appeal['question']}",
            f"Причина: {labels[appeal['reason']]}",
            "Уточнения: " + ("; ".join(appeal["clarifications"]) or "нет"),
            "Условия: " + ("; ".join(f"{key}={value}" for key, value in appeal["facts"].items()) or "нет"),
            f"Подразделение: {appeal['department'] or 'не указано'}",
            f"Этап: {appeal['stage'] or 'не указан'}",
        ]
        if config.public_url:
            text.append("Служба качества: " + config.public_url + "/docs?" + urlencode({"appeal": appeal["id"]})
                        + "#/quality")
        message.set_content("\n".join(text))

        connection = None
        sending = False
        try:
            context = None
            if config.ssl or config.tls:
                context = ssl.create_default_context()
                # Keep system and custom roots and add the public CA bundle.
                context.load_verify_locations(cafile=certifi.where())
            if config.ssl:
                connection = smtplib.SMTP_SSL(config.host, config.port, timeout=10, context=context)
            else:
                connection = smtplib.SMTP(config.host, config.port, timeout=10)
                if config.tls:
                    connection.starttls(context=context)
            if config.username:
                connection.login(config.username, config.password)
            sending = True
            connection.send_message(message, from_addr=config.sender, to_addrs=[config.recipient])
        except smtplib.SMTPRecipientsRefused as error:
            if any(400 <= code < 500 for code, _ in error.recipients.values()):
                raise MailRetry from None
            raise MailFailed from None
        except smtplib.SMTPResponseException as error:
            if 400 <= error.smtp_code < 500:
                raise MailRetry from None
            raise MailFailed from None
        except (OSError, smtplib.SMTPException):
            raise (MailUncertain if sending else MailRetry) from None
        finally:
            if connection:
                connection.close()


def deliver_one(quality):
    if quality.mailer is None:
        return
    now, lease = time(), str(uuid4())
    with quality.store.write() as db:
        db.execute("UPDATE quality_mail SET status='uncertain', lease=NULL WHERE status='sending' AND due<=?", (now,))
        row = db.execute("""
            SELECT appeal_id, attempts FROM quality_mail WHERE status IN ('pending','retry') AND due<=?
            ORDER BY due LIMIT 1
        """, (now,)).fetchone()
        if row is None:
            return
        key, attempt = row["appeal_id"], row["attempts"] + 1
        db.execute("UPDATE quality_mail SET status='sending', attempts=?,due=?,lease=? WHERE appeal_id=?",
                   (attempt, now + 120, lease, key))
    try:
        quality.mailer.send(quality.detail(key))
    except MailFailed:
        status = "failed"
    except MailRetry:
        status = "retry" if attempt < 3 else "failed"
    except Exception:
        status = "uncertain"
    else:
        status = "sent"
    with quality.store.write() as db:
        db.execute("UPDATE quality_mail SET status=?,due=?,lease=NULL WHERE appeal_id=? AND lease=?",
                   (status, time() + 2 ** attempt, key, lease))
