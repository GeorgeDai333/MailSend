import datetime
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import google_oauth
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


class GoogleAssistantTests(TestCase):
    """Assistants invited by Google address rather than issued a password."""

    def setUp(self):
        self.executive = make_executive()
        self.client.force_login(self.executive)

    def _invite(self, email="helper@gmail.com"):
        return self.client.post(
            reverse("exec_workers"),
            {"invite": "1", "email": email, "display_name": "Helper"},
            follow=True,
        )

    def _sign_in_as(self, email, name="Helper"):
        """Drive the real callback with Google's network calls stubbed."""
        self.client.logout()
        session = self.client.session
        session["google_oauth_state"] = "state123"
        session.save()
        with mock.patch.object(
            google_oauth, "exchange_code", return_value={"access_token": "at"}
        ), mock.patch.object(
            google_oauth, "fetch_userinfo", return_value={"email": email, "name": name}
        ):
            return self.client.get(
                reverse("google_callback") + "?code=abc&state=state123", follow=True
            )

    def test_invited_assistant_is_created_without_a_password(self):
        self._invite()
        worker = User.objects.get(email="helper@gmail.com")
        self.assertEqual(worker.role, User.Role.ASSISTANT)
        self.assertEqual(worker.executive, self.executive)
        self.assertEqual(worker.auth_method, User.AuthMethod.GOOGLE)
        self.assertFalse(worker.has_usable_password())
        self.assertTrue(worker.invitation_pending)

    def test_invited_assistant_can_sign_in_with_google(self):
        self._invite()
        response = self._sign_in_as("helper@gmail.com")
        self.assertEqual(response.redirect_chain[-1][0], reverse("assistant_dashboard"))
        worker = User.objects.get(email="helper@gmail.com")
        self.assertEqual(worker.role, User.Role.ASSISTANT)
        self.assertFalse(worker.invitation_pending)

    def test_assistant_google_login_stores_no_gmail_tokens(self):
        """An assistant's grant is identity-only and must not be retained."""
        self._invite()
        self._sign_in_as("helper@gmail.com")
        worker = User.objects.get(email="helper@gmail.com")
        self.assertFalse(GoogleCredential.objects.filter(user=worker).exists())

    def test_assistant_signing_in_still_cannot_send(self):
        self._invite()
        email = make_email(self.executive, self.executive, days_offset=-1)
        self._sign_in_as("helper@gmail.com")
        response = self.client.post(reverse("exec_send_one", args=[email.pk]))
        self.assertEqual(response.status_code, 403)
        email.refresh_from_db()
        self.assertEqual(email.status, Email.Status.DRAFT)

    def test_assistant_start_url_requests_identity_scopes_only(self):
        response = self.client.get(reverse("google_start") + "?role=assistant")
        self.assertNotIn("gmail.send", response.url)
        response = self.client.get(reverse("google_start"))
        self.assertIn("gmail.send", response.url)

    def test_cannot_invite_an_existing_executive(self):
        response = self._invite(self.executive.email)
        self.assertContains(response, "already has an executive account")
        self.assertEqual(self.executive.assistants.count(), 0)

    def test_cannot_invite_the_same_person_twice(self):
        self._invite()
        response = self._invite()
        self.assertContains(response, "already has an assistant account")
        self.assertEqual(self.executive.assistants.count(), 1)

    def test_allowlist_does_not_block_an_invited_assistant(self):
        """The allowlist gates new executive sign-ups, not invited assistants."""
        self._invite()
        with self.settings(GOOGLE_ALLOWED_EMAILS=["boss@example.com"]):
            response = self._sign_in_as("helper@gmail.com")
        self.assertEqual(response.redirect_chain[-1][0], reverse("assistant_dashboard"))

    def test_allowlist_still_blocks_an_uninvited_stranger(self):
        with self.settings(GOOGLE_ALLOWED_EMAILS=["boss@example.com"]):
            response = self._sign_in_as("stranger@gmail.com")
        self.assertContains(response, "not permitted")
        self.assertFalse(User.objects.filter(email="stranger@gmail.com").exists())

    def test_password_worker_cannot_sign_in_via_google(self):
        make_assistant(self.executive, username="pw@gmail.com")
        User.objects.filter(username="pw@gmail.com").update(email="pw@gmail.com")
        response = self._sign_in_as("pw@gmail.com")
        self.assertContains(response, "username and password")

    def test_no_password_change_page_for_google_assistants(self):
        self._invite()
        worker = User.objects.get(email="helper@gmail.com")
        response = self.client.get(
            reverse("exec_worker_password", args=[worker.pk]), follow=True
        )
        self.assertContains(response, "no password to change")

    def test_deactivated_assistant_is_refused(self):
        self._invite()
        User.objects.filter(email="helper@gmail.com").update(is_active=False)
        response = self._sign_in_as("helper@gmail.com")
        self.assertContains(response, "disabled")


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
