from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from .settings import SmtpSettings


def send_html_email(
    settings: SmtpSettings,
    recipient: str,
    subject: str,
    html_body: str,
) -> None:
    message = EmailMessage()
    message["From"] = formataddr((settings.from_name, settings.from_email))
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content("Для просмотра письма требуется почтовый клиент с поддержкой HTML.")
    message.add_alternative(html_body, subtype="html")

    if settings.mode == "ssl":
        smtp = smtplib.SMTP_SSL(settings.host, settings.port, timeout=30)
    else:
        smtp = smtplib.SMTP(settings.host, settings.port, timeout=30)

    try:
        smtp.ehlo()
        if settings.mode == "starttls":
            smtp.starttls()
            smtp.ehlo()
        if settings.username:
            smtp.login(settings.username, settings.password)
        smtp.send_message(message)
    finally:
        smtp.quit()
