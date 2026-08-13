"""Send drafts that are marked auto-send and whose send date has passed.

Run this on a schedule (cron / launchd) to make the "Schedule" checkbox work:

    */10 * * * *  cd /path/to/MailSend && .venv/bin/python manage.py send_due
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.gmail import SendError, send_email
from core.models import Email


class Command(BaseCommand):
    help = "Send scheduled drafts whose send date has passed."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be sent without sending it.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        due = Email.objects.filter(
            status=Email.Status.DRAFT, auto_send=True, send_date__lte=now
        )

        if not due.exists():
            self.stdout.write("Nothing due.")
            return

        for email in due:
            label = f"[{email.pk}] {email.summary_subject} → {email.to_summary}"
            if options["dry_run"]:
                self.stdout.write(f"would send {label}")
                continue
            try:
                delivered = send_email(email)
            except SendError as exc:
                self.stderr.write(self.style.ERROR(f"failed {label}: {exc}"))
            else:
                self.stdout.write(
                    self.style.SUCCESS(f"sent {label} ({delivered} message(s))")
                )
