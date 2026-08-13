"""Sending mail through the executive's own Gmail account."""

import base64
import mimetypes
from email.message import EmailMessage as MIMEEmailMessage

from django.utils import timezone
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .google_oauth import GoogleAuthError, credentials_for
from .models import Email, SendLog


class SendError(Exception):
    pass


def _gmail_service(executive):
    credentials = credentials_for(executive)
    # cache_discovery=False avoids a noisy warning when no file cache exists.
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _compose_body(email, signature, row=None):
    """Render subject and body, applying merge fields and the signature."""
    if row is not None:
        subject = Email.render_template(email.subject, row)
        body = Email.render_template(email.body, row)
    else:
        subject = email.subject
        body = email.body
    if signature:
        body = f"{body}\n\n{signature}"
    return subject, body


def _build_mime(from_address, to_list, cc_list, bcc_list, subject, body, attachments):
    message = MIMEEmailMessage()
    message["From"] = from_address
    if to_list:
        message["To"] = ", ".join(to_list)
    else:
        # A BCC-only message would otherwise carry an empty "To:" header,
        # which spam filters treat as malformed bulk mail. This is the RFC 5322
        # empty-group form: valid, delivers to nobody, and displays tidily.
        message["To"] = "undisclosed-recipients:;"
    if cc_list:
        message["Cc"] = ", ".join(cc_list)
    if bcc_list:
        message["Bcc"] = ", ".join(bcc_list)
    message["Subject"] = subject
    message.set_content(body or "")

    for attachment in attachments:
        guessed, _ = mimetypes.guess_type(attachment.original_name)
        maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
        attachment.file.open("rb")
        try:
            payload = attachment.file.read()
        finally:
            attachment.file.close()
        message.add_attachment(
            payload,
            maintype=maintype,
            subtype=subtype or "octet-stream",
            filename=attachment.original_name,
        )
    return message


def _raw(message):
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def send_email(email, triggered_by=None):
    """Send one Email object. Returns the number of messages delivered.

    A mail merge sends one personalised message per CSV row; a normal email
    sends a single message to all its recipients.
    """
    if email.status == Email.Status.SENT:
        raise SendError("This message has already been sent.")

    executive = email.executive
    try:
        service = _gmail_service(executive)
    except GoogleAuthError as exc:
        raise SendError(str(exc)) from exc

    from_address = executive.email
    if not from_address:
        raise SendError("The executive's account has no Google email address on file.")

    signature = executive.signature
    attachments = list(email.attachments.all())
    delivered = 0
    failures = []

    if email.is_mail_merge:
        rows = email.merge_rows()
        if not rows:
            raise SendError("The mail merge CSV is empty or could not be read.")
        targets = []
        for row in rows:
            recipient = Email.recipient_from_row(row)
            if not recipient:
                failures.append("A CSV row has no email/to column value.")
                continue
            targets.append((recipient, row))
        if not targets:
            raise SendError(
                "No recipients found in the CSV — it needs an 'email' or 'to' column."
            )
    else:
        if not email.to_list and not email.cc_list and not email.bcc_list:
            raise SendError("This message has no recipients.")
        targets = [(None, None)]

    for recipient, row in targets:
        if row is not None:
            subject, body = _compose_body(email, signature, row)
            to_list, cc_list, bcc_list = [recipient], [], []
        else:
            subject, body = _compose_body(email, signature)
            to_list, cc_list, bcc_list = email.to_list, email.cc_list, email.bcc_list

        message = _build_mime(
            from_address, to_list, cc_list, bcc_list, subject, body, attachments
        )
        try:
            result = (
                service.users()
                .messages()
                .send(userId="me", body={"raw": _raw(message)})
                .execute()
            )
        except HttpError as exc:
            detail = getattr(exc, "reason", None) or str(exc)
            failures.append(f"{', '.join(to_list) or 'message'}: {detail}")
            SendLog.objects.create(
                email=email,
                recipient=", ".join(to_list),
                subject=subject,
                succeeded=False,
                detail=detail,
                triggered_by=triggered_by,
            )
            continue
        except Exception as exc:
            failures.append(f"{', '.join(to_list) or 'message'}: {exc}")
            SendLog.objects.create(
                email=email,
                recipient=", ".join(to_list),
                subject=subject,
                succeeded=False,
                detail=str(exc),
                triggered_by=triggered_by,
            )
            continue

        delivered += 1
        SendLog.objects.create(
            email=email,
            recipient=", ".join(to_list + cc_list + bcc_list),
            subject=subject,
            succeeded=True,
            gmail_message_id=result.get("id", ""),
            triggered_by=triggered_by,
        )

    if delivered == 0:
        email.status = Email.Status.FAILED
        email.error = "\n".join(failures) or "Unknown error."
        email.save(update_fields=["status", "error", "updated_at"])
        raise SendError(email.error)

    email.status = Email.Status.SENT
    email.sent_at = timezone.now()
    email.sent_count = delivered
    # Partial failure on a merge is worth surfacing without blocking the rest.
    email.error = "\n".join(failures)
    email.save(
        update_fields=["status", "sent_at", "sent_count", "error", "updated_at"]
    )
    return delivered
