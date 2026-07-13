from __future__ import annotations

import email
import hashlib
import imaplib
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path

from .settings import ImapSettings


@dataclass
class AttachmentResult:
    uid: str
    message_date: str
    sender: str
    subject: str
    filename: str
    file_hash: str
    saved_path: Path


def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    result: list[str] = []
    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            result.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def fetch_latest_attachment(
    settings: ImapSettings,
    expected_filename: str,
    save_dir: Path,
) -> AttachmentResult:
    save_dir.mkdir(parents=True, exist_ok=True)

    client_cls = imaplib.IMAP4_SSL if settings.ssl else imaplib.IMAP4
    with client_cls(settings.host, settings.port) as imap:
        imap.login(settings.username, settings.password)
        status, _ = imap.select(settings.folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Не удалось открыть папку {settings.folder} в режиме readonly")

        since = (datetime.now() - timedelta(days=settings.lookback_days)).strftime("%d-%b-%Y")
        status, data = imap.uid(
            "search",
            None,
            "SINCE",
            since,
            "FROM",
            f'"{settings.from_contains}"',
        )
        if status != "OK":
            raise RuntimeError("Ошибка поиска письма по IMAP")

        uids = data[0].split()
        if not uids:
            raise FileNotFoundError("Подходящие письма не найдены")

        for uid_bytes in reversed(uids):
            uid = uid_bytes.decode()
            status, message_data = imap.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not message_data:
                continue

            raw = None
            for item in message_data:
                if isinstance(item, tuple) and isinstance(item[1], bytes):
                    raw = item[1]
                    break
            if raw is None:
                continue

            message = email.message_from_bytes(raw)
            sender = decode_mime(message.get("From"))
            subject = decode_mime(message.get("Subject"))
            message_date = decode_mime(message.get("Date"))

            for part in message.walk():
                filename = decode_mime(part.get_filename())
                if filename != expected_filename:
                    continue
                payload = part.get_payload(decode=True)
                if not payload:
                    continue

                digest = hashlib.sha256(payload).hexdigest()
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                target = save_dir / f"{timestamp}_{expected_filename}"
                target.write_bytes(payload)

                return AttachmentResult(
                    uid=uid,
                    message_date=message_date,
                    sender=sender,
                    subject=subject,
                    filename=filename,
                    file_hash=digest,
                    saved_path=target,
                )

        raise FileNotFoundError(f"В найденных письмах нет вложения {expected_filename}")
