import csv
import io
import re

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone

# Recipients are stored as free text so the compose box behaves like Gmail's.
# Split on commas, semicolons and newlines when we actually need a list.
_ADDRESS_SPLIT = re.compile(r"[,;\n]+")


def split_addresses(raw):
    """Turn a free-text recipient field into a clean list of addresses."""
    if not raw:
        return []
    return [addr.strip() for addr in _ADDRESS_SPLIT.split(raw) if addr.strip()]


class User(AbstractUser):
    """A MailSend account.

    An executive signs in with Google and is the identity that mail is sent
    from. An assistant signs in with a username and password issued by their
    executive, and can never send anything.
    """

    class Role(models.TextChoices):
        EXECUTIVE = "EXEC", "Executive"
        ASSISTANT = "ASST", "Assistant"

    class AuthMethod(models.TextChoices):
        PASSWORD = "password", "Username and password"
        GOOGLE = "google", "Sign in with Google"

    role = models.CharField(max_length=4, choices=Role.choices, default=Role.EXECUTIVE)
    # Executives are always Google. Assistants can be either: invited by email
    # (Google) or issued credentials directly (password), so an assistant
    # without a Google account is never locked out.
    auth_method = models.CharField(
        max_length=10, choices=AuthMethod.choices, default=AuthMethod.PASSWORD
    )
    # Set only on assistants: the executive whose mailbox they draft for.
    executive = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="assistants",
        limit_choices_to={"role": Role.EXECUTIVE},
    )
    # Appended to the bottom of every message this executive sends.
    signature = models.TextField(blank=True)
    display_name = models.CharField(max_length=200, blank=True)

    @property
    def is_executive(self):
        return self.role == self.Role.EXECUTIVE

    @property
    def is_assistant(self):
        return self.role == self.Role.ASSISTANT

    @property
    def mailbox_owner(self):
        """The executive whose mail this user works on."""
        return self if self.is_executive else self.executive

    @property
    def friendly_name(self):
        return self.display_name or self.get_full_name() or self.email or self.username

    @property
    def signs_in_with_google(self):
        return self.auth_method == self.AuthMethod.GOOGLE

    @property
    def invitation_pending(self):
        """A Google assistant who has been added but has never signed in."""
        return self.signs_in_with_google and self.last_login is None

    def __str__(self):
        return f"{self.friendly_name} ({self.get_role_display()})"


class GoogleCredential(models.Model):
    """OAuth tokens for an executive's Google account.

    The refresh token is the durable part: Google only returns it on the first
    consent (or when we force `prompt=consent`), so we never overwrite a stored
    one with a blank value.
    """

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="google_credential"
    )
    access_token = models.TextField(blank=True)
    refresh_token = models.TextField(blank=True)
    token_expiry = models.DateTimeField(null=True, blank=True)
    scopes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def scope_list(self):
        return self.scopes.split()

    @property
    def can_send(self):
        return bool(self.refresh_token) and any(
            s.endswith("gmail.send") or s.endswith("mail.google.com/")
            for s in self.scope_list
        )

    def __str__(self):
        return f"Google credential for {self.user.username}"


class EmailQuerySet(models.QuerySet):
    def drafts(self):
        return self.filter(status=Email.Status.DRAFT)

    def current(self, now=None):
        """Drafts dated at or before now — the 'ready to go out' set."""
        return self.drafts().filter(send_date__lte=now or timezone.now())

    def future(self, now=None):
        return self.drafts().filter(send_date__gt=now or timezone.now())


class Email(models.Model):
    """The core MailSend object: a message drafted for an executive to send."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    # The mailbox this goes out from. Always an executive.
    executive = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="emails"
    )
    # Whoever drafted it — usually the assistant.
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="drafted_emails"
    )

    to = models.TextField("To", blank=True)
    cc = models.TextField("CC", blank=True)
    bcc = models.TextField("BCC", blank=True)
    subject = models.CharField(max_length=500, blank=True)
    body = models.TextField(blank=True)
    send_date = models.DateTimeField(default=timezone.now)

    # "Schedule" checkbox: let the background job send this automatically once
    # send_date passes, instead of waiting for the executive to click Send.
    auto_send = models.BooleanField(default=False)

    is_mail_merge = models.BooleanField(default=False)
    merge_csv = models.FileField(upload_to="merge/", blank=True, null=True)

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.DRAFT
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EmailQuerySet.as_manager()

    class Meta:
        ordering = ["send_date", "id"]

    def __str__(self):
        return self.subject or "(no subject)"

    # --- recipients ---------------------------------------------------------
    @property
    def to_list(self):
        return split_addresses(self.to)

    @property
    def cc_list(self):
        return split_addresses(self.cc)

    @property
    def bcc_list(self):
        return split_addresses(self.bcc)

    # --- display helpers ----------------------------------------------------
    @property
    def summary_subject(self):
        """First 100 characters of the subject, per the spec."""
        subject = self.subject or "(no subject)"
        return subject[:100] + ("…" if len(subject) > 100 else "")

    @property
    def to_summary(self):
        addresses = self.to_list
        if not addresses:
            return "(no recipients)"
        if len(addresses) == 1:
            return addresses[0]
        return f"{addresses[0]} +{len(addresses) - 1} more"

    @property
    def is_due(self):
        return self.status == self.Status.DRAFT and self.send_date <= timezone.now()

    @property
    def is_editable(self):
        return self.status == self.Status.DRAFT

    # --- mail merge ---------------------------------------------------------
    def merge_rows(self):
        """Parse the merge CSV into a list of dicts.

        Each row needs a recipient address; we look for an `email` or `to`
        column, case-insensitively.
        """
        if not (self.is_mail_merge and self.merge_csv):
            return []
        self.merge_csv.open("rb")
        try:
            raw = self.merge_csv.read()
        finally:
            self.merge_csv.close()
        text = raw.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for row in reader:
            clean = {
                (k or "").strip(): (v or "").strip()
                for k, v in row.items()
                if k is not None
            }
            if any(clean.values()):
                rows.append(clean)
        return rows

    @staticmethod
    def recipient_from_row(row):
        for key, value in row.items():
            if key.lower() in ("email", "to", "e-mail", "email address"):
                return value
        return ""

    @staticmethod
    def render_template(text, row):
        """Substitute {{column}} placeholders with that row's value.

        Unknown placeholders are replaced with an empty string so a missing
        column never leaks `{{first_name}}` into a real email.
        """
        lookup = {k.lower(): v for k, v in row.items()}

        def replace(match):
            return lookup.get(match.group(1).strip().lower(), "")

        return re.sub(r"\{\{\s*([^}]+?)\s*\}\}", replace, text or "")

    def merge_preview(self, limit=3):
        previews = []
        for row in self.merge_rows()[:limit]:
            previews.append(
                {
                    "to": self.recipient_from_row(row),
                    "subject": self.render_template(self.subject, row),
                    "body": self.render_template(self.body, row),
                }
            )
        return previews

    @property
    def merge_recipient_count(self):
        if not self.is_mail_merge:
            return 0
        try:
            return len([r for r in self.merge_rows() if self.recipient_from_row(r)])
        except Exception:
            return 0


class Attachment(models.Model):
    email = models.ForeignKey(
        Email, on_delete=models.CASCADE, related_name="attachments"
    )
    file = models.FileField(upload_to="attachments/%Y/%m/")
    original_name = models.CharField(max_length=255)
    size = models.PositiveIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.original_name

    @property
    def human_size(self):
        size = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"


class SendLog(models.Model):
    """One row per delivery attempt, for the Sent tab and for debugging."""

    email = models.ForeignKey(Email, on_delete=models.CASCADE, related_name="logs")
    recipient = models.CharField(max_length=500)
    subject = models.CharField(max_length=500, blank=True)
    succeeded = models.BooleanField(default=True)
    detail = models.TextField(blank=True)
    gmail_message_id = models.CharField(max_length=128, blank=True)
    triggered_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.recipient} — {'ok' if self.succeeded else 'failed'}"
