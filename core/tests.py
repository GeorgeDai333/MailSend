import datetime
import io
import zipfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .attachments import GMAIL_BLOCKED_EXTENSIONS, rejection_reason
from .models import Attachment, Email, GoogleCredential, User


def make_executive(email="boss@example.com"):
    executive = User.objects.create(
        username=email, email=email, role=User.Role.EXECUTIVE, display_name="The Boss"
    )
    GoogleCredential.objects.create(
        user=executive,
        refresh_token="fake-refresh",
        scopes="openid https://www.googleapis.com/auth/gmail.send",
    )
    return executive


def make_assistant(executive, username="worker1", password="worker-pass-123"):
    assistant = User(
        username=username, role=User.Role.ASSISTANT, executive=executive
    )
    assistant.set_password(password)
    assistant.save()
    return assistant


def make_email(executive, created_by, days_offset=0, **kwargs):
    defaults = {
        "to": "recipient@example.com",
        "subject": f"Message {days_offset}",
        "body": "Hello there.",
        "send_date": timezone.now() + datetime.timedelta(days=days_offset),
    }
    defaults.update(kwargs)
    return Email.objects.create(
        executive=executive, created_by=created_by, **defaults
    )


class RoleAccessTests(TestCase):
    def setUp(self):
        self.executive = make_executive()
        self.assistant = make_assistant(self.executive)

    def test_assistant_cannot_reach_executive_pages(self):
        self.client.force_login(self.assistant)
        for name in ["exec_dashboard", "exec_sent", "exec_signature", "exec_workers"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)

    def test_assistant_cannot_send(self):
        """The whole point of the product: no send path exists for assistants."""
        email = make_email(self.executive, self.assistant)
        self.client.force_login(self.assistant)
        response = self.client.post(reverse("exec_send_one", args=[email.pk]))
        self.assertEqual(response.status_code, 403)
        email.refresh_from_db()
        self.assertEqual(email.status, Email.Status.DRAFT)

    def test_assistant_cannot_send_current_batch(self):
        make_email(self.executive, self.assistant, days_offset=-1)
        self.client.force_login(self.assistant)
        self.assertEqual(
            self.client.post(reverse("exec_send_current")).status_code, 403
        )

    def test_assistant_cannot_touch_another_executives_mail(self):
        other_exec = make_executive("other@example.com")
        other_email = make_email(other_exec, other_exec)
        self.client.force_login(self.assistant)
        self.assertEqual(
            self.client.get(reverse("email_edit", args=[other_email.pk])).status_code,
            404,
        )

    def test_executive_cannot_manage_another_executives_worker(self):
        other_exec = make_executive("other@example.com")
        foreign_worker = make_assistant(other_exec, username="worker2")
        self.client.force_login(self.executive)
        response = self.client.post(
            reverse("exec_worker_delete", args=[foreign_worker.pk])
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(User.objects.filter(pk=foreign_worker.pk).exists())


class DraftingTests(TestCase):
    def setUp(self):
        self.executive = make_executive()
        self.assistant = make_assistant(self.executive)
        self.client.force_login(self.assistant)

    def test_assistant_creates_draft_with_attachment(self):
        response = self.client.post(
            reverse("email_create"),
            {
                "to": "someone@example.com",
                "cc": "",
                "bcc": "",
                "subject": "Quarterly note",
                "body": "Body text",
                "send_date": "2026-12-01T09:00",
                "attachments": SimpleUploadedFile("notes.txt", b"file contents"),
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        email = Email.objects.get()
        self.assertEqual(email.executive, self.executive)
        self.assertEqual(email.created_by, self.assistant)
        self.assertEqual(email.status, Email.Status.DRAFT)
        self.assertEqual(email.attachments.count(), 1)
        self.assertEqual(email.attachments.get().original_name, "notes.txt")

    def test_draft_requires_a_recipient(self):
        response = self.client.post(
            reverse("email_create"),
            {"to": "", "cc": "", "bcc": "", "subject": "x", "body": "y",
             "send_date": "2026-12-01T09:00"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Email.objects.count(), 0)
        self.assertContains(response, "at least one recipient")

    def test_assistant_deletes_own_draft(self):
        email = make_email(self.executive, self.assistant)
        self.client.post(reverse("email_delete", args=[email.pk]))
        self.assertFalse(Email.objects.filter(pk=email.pk).exists())

    def test_sent_mail_is_not_editable(self):
        email = make_email(self.executive, self.assistant, status=Email.Status.SENT)
        self.assertEqual(
            self.client.get(reverse("email_edit", args=[email.pk])).status_code, 404
        )


class ExecutiveScopeTests(TestCase):
    """The Edit Current / Edit Future / Edit All filters from the spec."""

    def setUp(self):
        self.executive = make_executive()
        self.assistant = make_assistant(self.executive)
        self.past = make_email(self.executive, self.assistant, days_offset=-2)
        self.today = make_email(self.executive, self.assistant, days_offset=0)
        self.future = make_email(self.executive, self.assistant, days_offset=3)
        self.client.force_login(self.executive)

    def _scope_ids(self, scope):
        response = self.client.get(reverse("exec_edit", args=[scope]))
        self.assertEqual(response.status_code, 200)
        return {form.instance.pk for form in response.context["formset"]}

    def test_current_shows_only_past_and_today(self):
        self.assertEqual(
            self._scope_ids("current"), {self.past.pk, self.today.pk}
        )

    def test_future_shows_only_future(self):
        self.assertEqual(self._scope_ids("future"), {self.future.pk})

    def test_all_shows_everything(self):
        self.assertEqual(
            self._scope_ids("all"),
            {self.past.pk, self.today.pk, self.future.pk},
        )

    def test_unknown_scope_is_404(self):
        self.assertEqual(
            self.client.get(reverse("exec_edit", args=["sideways"])).status_code, 404
        )

    def test_executive_can_edit_every_field_sequentially(self):
        response = self.client.get(reverse("exec_edit", args=["all"]))
        formset = response.context["formset"]
        data = {
            "form-TOTAL_FORMS": str(len(formset.forms)),
            "form-INITIAL_FORMS": str(len(formset.forms)),
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
        }
        for index, form in enumerate(formset.forms):
            data[f"form-{index}-id"] = str(form.instance.pk)
            data[f"form-{index}-to"] = "changed@example.com"
            data[f"form-{index}-cc"] = ""
            data[f"form-{index}-bcc"] = ""
            data[f"form-{index}-subject"] = f"Rewritten {index}"
            data[f"form-{index}-body"] = "Executive rewrote this."
            data[f"form-{index}-send_date"] = timezone.localtime(
                form.instance.send_date
            ).strftime("%Y-%m-%dT%H:%M")

        response = self.client.post(reverse("exec_edit", args=["all"]), data)
        self.assertEqual(response.status_code, 302)
        self.past.refresh_from_db()
        self.assertEqual(self.past.to, "changed@example.com")
        self.assertEqual(self.past.body, "Executive rewrote this.")


class SendingTests(TestCase):
    def setUp(self):
        self.executive = make_executive()
        self.assistant = make_assistant(self.executive)
        self.client.force_login(self.executive)

    def test_send_current_sends_only_due_messages(self):
        past = make_email(self.executive, self.assistant, days_offset=-2)
        due_now = make_email(self.executive, self.assistant, days_offset=0)
        future = make_email(self.executive, self.assistant, days_offset=5)

        sent = []

        def fake_send(email, triggered_by=None):
            sent.append(email.pk)
            email.status = Email.Status.SENT
            email.sent_at = timezone.now()
            email.sent_count = 1
            email.save()
            return 1

        with mock.patch("core.views.send_email", side_effect=fake_send):
            self.client.post(reverse("exec_send_current"))

        self.assertCountEqual(sent, [past.pk, due_now.pk])
        future.refresh_from_db()
        self.assertEqual(future.status, Email.Status.DRAFT)

    def test_executive_can_send_a_future_message_individually(self):
        future = make_email(self.executive, self.assistant, days_offset=10)
        with mock.patch("core.views.send_email", return_value=1) as sender:
            response = self.client.post(reverse("exec_send_one", args=[future.pk]))
        self.assertEqual(response.status_code, 302)
        sender.assert_called_once()
        self.assertEqual(sender.call_args.args[0].pk, future.pk)

    def test_send_failure_surfaces_to_the_executive(self):
        email = make_email(self.executive, self.assistant, days_offset=-1)
        from .gmail import SendError

        with mock.patch(
            "core.views.send_email", side_effect=SendError("token revoked")
        ):
            response = self.client.post(
                reverse("exec_send_one", args=[email.pk]), follow=True
            )
        self.assertContains(response, "token revoked")

    def test_gmail_payload_includes_signature_and_attachment(self):
        self.executive.signature = "— Sent by the Boss"
        self.executive.save()
        email = make_email(self.executive, self.assistant, days_offset=-1)
        Attachment.objects.create(
            email=email,
            file=SimpleUploadedFile("report.txt", b"quarterly numbers"),
            original_name="report.txt",
            size=17,
        )

        captured = {}

        class FakeMessages:
            def send(self, userId, body):
                captured["raw"] = body["raw"]

                class Execute:
                    def execute(inner):
                        return {"id": "gmail-123"}

                return Execute()

        class FakeUsers:
            def messages(self):
                return FakeMessages()

        class FakeService:
            def users(self):
                return FakeUsers()

        from . import gmail

        with mock.patch.object(gmail, "_gmail_service", return_value=FakeService()):
            delivered = gmail.send_email(email, triggered_by=self.executive)

        self.assertEqual(delivered, 1)
        import base64

        decoded = base64.urlsafe_b64decode(captured["raw"]).decode(errors="replace")
        self.assertIn("From: boss@example.com", decoded)
        self.assertIn("To: recipient@example.com", decoded)
        self.assertIn("Sent by the Boss", decoded)
        self.assertIn("report.txt", decoded)

        email.refresh_from_db()
        self.assertEqual(email.status, Email.Status.SENT)
        self.assertEqual(email.logs.count(), 1)


class MailMergeTests(TestCase):
    def setUp(self):
        self.executive = make_executive()
        self.assistant = make_assistant(self.executive)

    def _merge_email(self):
        csv_bytes = (
            b"email,first_name,company\n"
            b"ann@example.com,Ann,Acme\n"
            b"bob@example.com,Bob,Globex\n"
        )
        return make_email(
            self.executive,
            self.assistant,
            to="",
            subject="Hello {{first_name}}",
            body="Hi {{first_name}}, how are things at {{company}}?",
            is_mail_merge=True,
            merge_csv=SimpleUploadedFile("list.csv", csv_bytes),
        )

    def test_rows_and_placeholders(self):
        email = self._merge_email()
        self.assertEqual(email.merge_recipient_count, 2)
        previews = email.merge_preview()
        self.assertEqual(previews[0]["to"], "ann@example.com")
        self.assertEqual(previews[0]["subject"], "Hello Ann")
        self.assertIn("Acme", previews[0]["body"])

    def test_unknown_placeholder_does_not_leak(self):
        email = self._merge_email()
        email.body = "Hi {{first_name}}, ref {{missing_column}}."
        rendered = Email.render_template(email.body, email.merge_rows()[0])
        self.assertEqual(rendered, "Hi Ann, ref .")
        self.assertNotIn("{{", rendered)

    def test_merge_sends_one_message_per_row(self):
        email = self._merge_email()
        recipients = []

        class FakeMessages:
            def send(self, userId, body):
                import base64

                decoded = base64.urlsafe_b64decode(body["raw"]).decode()
                for line in decoded.splitlines():
                    if line.startswith("To: "):
                        recipients.append(line[4:].strip())

                class Execute:
                    def execute(inner):
                        return {"id": "x"}

                return Execute()

        class FakeUsers:
            def messages(self):
                return FakeMessages()

        class FakeService:
            def users(self):
                return FakeUsers()

        from . import gmail

        with mock.patch.object(gmail, "_gmail_service", return_value=FakeService()):
            delivered = gmail.send_email(email)

        self.assertEqual(delivered, 2)
        self.assertCountEqual(
            recipients, ["ann@example.com", "bob@example.com"]
        )
        email.refresh_from_db()
        self.assertEqual(email.sent_count, 2)


CSV_BYTES = (
    b"email,first_name\n"
    b"ann@example.com,Ann\n"
    b'bob@example.com,"Smith, Bob"\n'
)


class MergeCsvFormTests(TestCase):
    """Uploading, replacing and clearing the recipient CSV."""

    def setUp(self):
        self.executive = make_executive()
        self.assistant = make_assistant(self.executive)
        self.client.force_login(self.assistant)

    def _base(self, **overrides):
        data = {
            "to": "",
            "cc": "",
            "bcc": "",
            "subject": "Hi {{first_name}}",
            "body": "Hello {{first_name}}",
            "send_date": "2026-12-01T09:00",
            "is_mail_merge": "on",
        }
        data.update(overrides)
        return data

    def test_upload_fills_the_to_field(self):
        self.client.post(
            reverse("email_create"),
            self._base(merge_csv=SimpleUploadedFile("list.csv", CSV_BYTES)),
        )
        email = Email.objects.get()
        self.assertEqual(email.to, "ann@example.com, bob@example.com")
        self.assertTrue(email.is_mail_merge)

    def test_new_upload_replaces_previous_recipients(self):
        self.client.post(
            reverse("email_create"),
            self._base(merge_csv=SimpleUploadedFile("a.csv", CSV_BYTES)),
        )
        email = Email.objects.get()
        replacement = b"email\ncarla@example.com\n"
        self.client.post(
            reverse("email_edit", args=[email.pk]),
            self._base(merge_csv=SimpleUploadedFile("b.csv", replacement)),
        )
        email.refresh_from_db()
        self.assertEqual(email.to, "carla@example.com")
        self.assertNotIn("ann@example.com", email.to)

    def test_clearing_empties_to_and_turns_off_mail_merge(self):
        self.client.post(
            reverse("email_create"),
            self._base(merge_csv=SimpleUploadedFile("a.csv", CSV_BYTES)),
        )
        email = Email.objects.get()
        self.assertTrue(email.merge_csv)

        response = self.client.post(
            reverse("email_edit", args=[email.pk]),
            self._base(**{"merge_csv-clear": "on"}),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        email.refresh_from_db()
        self.assertFalse(email.merge_csv)
        self.assertFalse(email.is_mail_merge)
        self.assertEqual(email.to, "")
        self.assertEqual(email.merge_recipient_count, 0)

    def test_upload_without_an_email_column_is_rejected(self):
        response = self.client.post(
            reverse("email_create"),
            self._base(
                merge_csv=SimpleUploadedFile("bad.csv", b"name,company\nAnn,Acme\n")
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "needs a header row")
        self.assertEqual(Email.objects.count(), 0)

    def test_mail_merge_without_any_csv_is_rejected(self):
        response = self.client.post(reverse("email_create"), self._base())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Upload a CSV")
        self.assertEqual(Email.objects.count(), 0)

    def test_duplicate_addresses_are_collapsed(self):
        raw = b"email\nsame@example.com\nSAME@example.com\nother@example.com\n"
        self.client.post(
            reverse("email_create"),
            self._base(merge_csv=SimpleUploadedFile("d.csv", raw)),
        )
        self.assertEqual(
            Email.objects.get().to, "same@example.com, other@example.com"
        )

    def test_saved_csv_still_parses_after_upload(self):
        """The form reads the upload stream, so it must rewind it."""
        self.client.post(
            reverse("email_create"),
            self._base(merge_csv=SimpleUploadedFile("a.csv", CSV_BYTES)),
        )
        email = Email.objects.get()
        self.assertEqual(email.merge_recipient_count, 2)
        self.assertEqual(email.merge_preview()[1]["subject"], "Hi Smith, Bob")


class AssistantAutoSendTests(TestCase):
    """Only the executive may let a message go out unattended."""

    def setUp(self):
        self.executive = make_executive()
        self.assistant = make_assistant(self.executive)

    def _payload(self, **extra):
        data = {
            "to": "someone@example.com",
            "cc": "",
            "bcc": "",
            "subject": "s",
            "body": "b",
            "send_date": "2026-12-01T09:00",
        }
        data.update(extra)
        return data

    def test_assistant_cannot_set_auto_send_even_by_posting_it(self):
        self.client.force_login(self.assistant)
        self.client.post(reverse("email_create"), self._payload(auto_send="on"))
        self.assertFalse(Email.objects.get().auto_send)

    def test_assistant_does_not_see_the_auto_send_control(self):
        self.client.force_login(self.assistant)
        response = self.client.get(reverse("email_create"))
        self.assertNotContains(response, "send date passes")

    def test_executive_can_still_set_auto_send(self):
        self.client.force_login(self.executive)
        self.client.post(reverse("email_create"), self._payload(auto_send="on"))
        self.assertTrue(Email.objects.get().auto_send)

    def test_assistant_edit_does_not_clear_an_executives_auto_send(self):
        email = make_email(
            self.executive, self.assistant, days_offset=1, auto_send=True
        )
        self.client.force_login(self.assistant)
        self.client.post(
            reverse("email_edit", args=[email.pk]),
            self._payload(subject="edited by assistant"),
        )
        email.refresh_from_db()
        self.assertEqual(email.subject, "edited by assistant")
        self.assertTrue(email.auto_send)


class BccHeaderTests(TestCase):
    """A BCC-only message must not go out with an empty To: header."""

    def test_bcc_only_message_uses_undisclosed_recipients(self):
        from .gmail import _build_mime

        message = _build_mime(
            "boss@example.com", [], [], ["hidden@example.com"], "Subj", "Body", []
        )
        self.assertEqual(message["To"], "undisclosed-recipients:;")
        self.assertNotIn("\nTo: \n", message.as_string())
        self.assertEqual(message["Bcc"], "hidden@example.com")

    def test_normal_message_keeps_its_real_to_header(self):
        from .gmail import _build_mime

        message = _build_mime(
            "boss@example.com", ["a@example.com"], [], ["b@example.com"],
            "Subj", "Body", [],
        )
        self.assertEqual(message["To"], "a@example.com")

    def test_cc_only_message_also_avoids_an_empty_to(self):
        from .gmail import _build_mime

        message = _build_mime(
            "boss@example.com", [], ["c@example.com"], [], "Subj", "Body", []
        )
        self.assertEqual(message["To"], "undisclosed-recipients:;")
        self.assertEqual(message["Cc"], "c@example.com")


class AttachmentSafetyTests(TestCase):
    def setUp(self):
        self.executive = make_executive()
        self.assistant = make_assistant(self.executive)
        self.client.force_login(self.assistant)

    def _post(self, upload):
        return self.client.post(
            reverse("email_create"),
            {
                "to": "someone@example.com",
                "cc": "",
                "bcc": "",
                "subject": "s",
                "body": "b",
                "send_date": "2026-12-01T09:00",
                "attachments": upload,
            },
            follow=True,
        )

    def test_python_file_is_allowed(self):
        """Deliberate: the blocklist mirrors Gmail, and Gmail permits .py."""
        self._post(SimpleUploadedFile("script.py", b"import os"))
        self.assertEqual(Email.objects.get().attachments.count(), 1)

    def test_executable_is_rejected(self):
        self._post(SimpleUploadedFile("virus.exe", b"MZ\x90\x00"))
        self.assertEqual(Email.objects.get().attachments.count(), 0)

    def test_double_extension_is_judged_on_the_final_one(self):
        self._post(SimpleUploadedFile("invoice.pdf.exe", b"MZ"))
        self.assertEqual(Email.objects.get().attachments.count(), 0)

    def test_extensionless_file_is_allowed(self):
        """Also deliberate: Gmail does not reject these, so neither do we."""
        self._post(SimpleUploadedFile("mystery", b"data"))
        self.assertEqual(Email.objects.get().attachments.count(), 1)

    def test_blocklist_matches_gmail_exactly(self):
        """Guards against quietly drifting away from Gmail's published list."""
        from .attachments import BLOCKED_EXTENSIONS

        self.assertEqual(BLOCKED_EXTENSIONS, GMAIL_BLOCKED_EXTENSIONS)
        self.assertEqual(len(GMAIL_BLOCKED_EXTENSIONS), 54)
        for permitted in [".py", ".sh", ".rb", ".pl", ".php", ".ts", ".jsx"]:
            with self.subTest(extension=permitted):
                self.assertNotIn(permitted, BLOCKED_EXTENSIONS)

    def test_ordinary_documents_are_allowed(self):
        for filename in ["report.pdf", "notes.docx", "budget.xlsx", "photo.png"]:
            with self.subTest(filename=filename):
                self.assertIsNone(
                    rejection_reason(SimpleUploadedFile(filename, b"data"))
                )

    def test_zip_containing_an_executable_is_rejected(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("readme.txt", "harmless")
            archive.writestr("payload.exe", "MZ")
        buffer.seek(0)
        response = self._post(SimpleUploadedFile("bundle.zip", buffer.read()))
        self.assertEqual(Email.objects.get().attachments.count(), 0)
        self.assertContains(response, "payload.exe")

    def test_clean_zip_is_allowed(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("report.pdf", "harmless")
        buffer.seek(0)
        self.assertIsNone(
            rejection_reason(SimpleUploadedFile("clean.zip", buffer.read()))
        )

    def test_corrupt_zip_is_not_treated_as_an_attack(self):
        self.assertIsNone(
            rejection_reason(SimpleUploadedFile("broken.zip", b"not really a zip"))
        )

    def test_case_is_ignored(self):
        self.assertIsNotNone(
            rejection_reason(SimpleUploadedFile("SCRIPT.VBS", b"x"))
        )
        self.assertIsNotNone(
            rejection_reason(SimpleUploadedFile("Setup.EXE", b"x"))
        )

    def test_every_extension_gmail_blocks_is_blocked_here(self):
        for extension in GMAIL_BLOCKED_EXTENSIONS:
            with self.subTest(extension=extension):
                self.assertIsNotNone(
                    rejection_reason(SimpleUploadedFile(f"file{extension}", b"x"))
                )

    def test_a_good_file_still_attaches_when_a_bad_one_is_rejected(self):
        response = self.client.post(
            reverse("email_create"),
            {
                "to": "someone@example.com",
                "cc": "",
                "bcc": "",
                "subject": "s",
                "body": "b",
                "send_date": "2026-12-01T09:00",
                "attachments": [
                    SimpleUploadedFile("good.pdf", b"pdf"),
                    SimpleUploadedFile("bad.exe", b"MZ"),
                ],
            },
            follow=True,
        )
        email = Email.objects.get()
        self.assertEqual(email.attachments.count(), 1)
        self.assertEqual(email.attachments.get().original_name, "good.pdf")
        self.assertContains(response, "Gmail blocks")


class ScheduledSendCommandTests(TestCase):
    def test_only_auto_send_drafts_that_are_due_go_out(self):
        executive = make_executive()
        assistant = make_assistant(executive)
        due_auto = make_email(executive, assistant, days_offset=-1, auto_send=True)
        due_manual = make_email(executive, assistant, days_offset=-1, auto_send=False)
        future_auto = make_email(executive, assistant, days_offset=4, auto_send=True)

        sent = []
        import io

        from django.core.management import call_command

        with mock.patch(
            "core.management.commands.send_due.send_email",
            side_effect=lambda e: sent.append(e.pk) or 1,
        ):
            call_command("send_due", stdout=io.StringIO(), stderr=io.StringIO())

        self.assertEqual(sent, [due_auto.pk])
        self.assertNotIn(due_manual.pk, sent)
        self.assertNotIn(future_auto.pk, sent)


class AuthFlowTests(TestCase):
    def test_oauth_callback_rejects_bad_state(self):
        response = self.client.get(
            reverse("google_callback") + "?code=abc&state=forged", follow=True
        )
        self.assertContains(response, "security check")

    def test_assistant_login_and_redirect(self):
        executive = make_executive()
        make_assistant(executive, username="worker1", password="worker-pass-123")
        response = self.client.post(
            reverse("login"),
            {"username": "worker1", "password": "worker-pass-123"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.redirect_chain[-1][0], reverse("assistant_dashboard"))

    def test_executive_dashboard_warns_without_gmail_scope(self):
        executive = User.objects.create(
            username="nogoogle@example.com",
            email="nogoogle@example.com",
            role=User.Role.EXECUTIVE,
        )
        self.client.force_login(executive)
        response = self.client.get(reverse("exec_dashboard"))
        self.assertFalse(response.context["can_send"])
        self.assertContains(response, "Reconnect your Google account")
