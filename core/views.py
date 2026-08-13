import secrets
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.db import transaction
from django.forms import modelformset_factory
from django.http import FileResponse, Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import google_oauth
from .forms import (
    EmailForm,
    SignatureForm,
    StyledLoginForm,
    WorkerCreateForm,
    WorkerPasswordForm,
)
from .gmail import SendError, send_email
from .models import Attachment, Email, User

OAUTH_STATE_KEY = "google_oauth_state"


# --- access control ---------------------------------------------------------
def executive_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_executive:
            return HttpResponseForbidden("This page is for executives only.")
        return view(request, *args, **kwargs)

    return wrapper


def assistant_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_assistant:
            return HttpResponseForbidden("This page is for assistants only.")
        return view(request, *args, **kwargs)

    return wrapper


def visible_emails(user):
    """Every email the given user is allowed to see."""
    mailbox = user.mailbox_owner
    if mailbox is None:
        return Email.objects.none()
    queryset = Email.objects.filter(executive=mailbox)
    if user.is_assistant:
        # Per the spec, an assistant's dashboard lists the emails they created.
        queryset = queryset.filter(created_by=user)
    return queryset


def get_editable_email(user, pk):
    email = get_object_or_404(visible_emails(user), pk=pk)
    if not email.is_editable:
        raise Http404("This message has already been sent.")
    return email


def save_attachments(request, email):
    """Attach any uploaded files, skipping ones that are too large."""
    for upload in request.FILES.getlist("attachments"):
        if upload.size > settings.MAX_ATTACHMENT_BYTES:
            messages.warning(
                request,
                f"“{upload.name}” is too large to attach and was skipped.",
            )
            continue
        Attachment.objects.create(
            email=email,
            file=upload,
            original_name=upload.name,
            size=upload.size,
        )


def report_csv_change(request, form):
    """Say out loud what uploading or clearing a merge CSV did to the draft."""
    if getattr(form, "csv_was_cleared", False):
        messages.info(
            request,
            "Recipient list removed. The To field was cleared and this is no "
            "longer a mail merge.",
        )
    elif getattr(form, "recipients_loaded", 0):
        count = form.recipients_loaded
        messages.info(
            request,
            f"Loaded {count} recipient{'s' if count != 1 else ''} from the CSV "
            "into the To field, replacing what was there.",
        )


# --- public / auth ----------------------------------------------------------
def landing(request):
    if request.user.is_authenticated:
        return redirect("home")
    return render(
        request,
        "landing.html",
        {"google_configured": google_oauth.is_configured()},
    )


class MailSendLoginView(LoginView):
    """Username + password login. Used by assistants (workers)."""

    template_name = "registration/login.html"
    form_class = StyledLoginForm
    redirect_authenticated_user = True

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["google_configured"] = google_oauth.is_configured()
        return context


@login_required
def home(request):
    if request.user.is_executive:
        return redirect("exec_dashboard")
    if request.user.executive_id is None:
        logout(request)
        messages.error(
            request, "This assistant account is not linked to an executive."
        )
        return redirect("login")
    return redirect("assistant_dashboard")


def logout_view(request):
    logout(request)
    return redirect("landing")


def google_start(request):
    """Kick off the Google consent screen."""
    if not google_oauth.is_configured():
        messages.error(
            request,
            "Google sign-in is not configured yet — set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET in the .env file.",
        )
        return redirect("landing")
    state = secrets.token_urlsafe(32)
    request.session[OAUTH_STATE_KEY] = state
    return redirect(google_oauth.build_auth_url(state))


def google_callback(request):
    """Where Google sends the executive back to after consent."""
    expected_state = request.session.pop(OAUTH_STATE_KEY, None)
    received_state = request.GET.get("state")

    if request.GET.get("error"):
        messages.error(
            request, f"Google sign-in was cancelled ({request.GET['error']})."
        )
        return redirect("landing")

    if not expected_state or expected_state != received_state:
        messages.error(
            request, "Google sign-in failed a security check. Please try again."
        )
        return redirect("landing")

    code = request.GET.get("code")
    if not code:
        messages.error(request, "Google did not return an authorization code.")
        return redirect("landing")

    try:
        payload = google_oauth.exchange_code(code)
        profile = google_oauth.fetch_userinfo(payload["access_token"])
    except (google_oauth.GoogleAuthError, KeyError) as exc:
        messages.error(request, f"Google sign-in failed: {exc}")
        return redirect("landing")

    email_address = (profile.get("email") or "").lower()
    if not email_address:
        messages.error(request, "Google did not share an email address.")
        return redirect("landing")

    allowed = settings.GOOGLE_ALLOWED_EMAILS
    if allowed and email_address not in allowed:
        messages.error(
            request, f"{email_address} is not permitted to use this MailSend instance."
        )
        return redirect("landing")

    user, created = User.objects.get_or_create(
        email__iexact=email_address,
        defaults={
            "username": email_address,
            "email": email_address,
            "role": User.Role.EXECUTIVE,
        },
    )
    if created or not user.display_name:
        user.display_name = profile.get("name", "") or user.display_name
        user.email = email_address
        user.save(update_fields=["display_name", "email"])

    if user.is_assistant:
        messages.error(
            request,
            "That Google account belongs to an assistant. Assistants sign in "
            "with the username and password issued by their executive.",
        )
        return redirect("login")

    google_oauth.store_tokens(user, payload, settings.GOOGLE_SCOPES)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")

    if created:
        messages.success(request, "Your MailSend account is ready.")
    return redirect("home")


# --- assistant --------------------------------------------------------------
@assistant_required
def assistant_dashboard(request):
    """The Outbox: every draft this assistant has prepared."""
    drafts = visible_emails(request.user).drafts()
    return render(
        request,
        "assistant/dashboard.html",
        {"drafts": drafts, "now": timezone.now()},
    )


@assistant_required
def assistant_sent(request):
    sent = (
        Email.objects.filter(
            executive=request.user.mailbox_owner, status=Email.Status.SENT
        )
        .order_by("-sent_at")
    )
    return render(request, "assistant/sent.html", {"emails": sent})


@login_required
def email_create(request):
    """Compose a new message. Assistants and executives both land here."""
    mailbox = request.user.mailbox_owner
    if mailbox is None:
        return HttpResponseForbidden("No mailbox is linked to this account.")

    if request.method == "POST":
        form = EmailForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            with transaction.atomic():
                email = form.save(commit=False)
                email.executive = mailbox
                email.created_by = request.user
                email.save()
                save_attachments(request, email)
            messages.success(request, "Draft saved.")
            report_csv_change(request, form)
            return redirect("home")
    else:
        form = EmailForm(user=request.user)

    return render(
        request,
        "emails/compose.html",
        {"form": form, "email": None, "title": "New message"},
    )


@login_required
def email_edit(request, pk):
    email = get_editable_email(request.user, pk)

    if request.method == "POST":
        form = EmailForm(
            request.POST, request.FILES, instance=email, user=request.user
        )
        if form.is_valid():
            with transaction.atomic():
                form.save()
                save_attachments(request, email)
            messages.success(request, "Changes saved.")
            report_csv_change(request, form)
            return redirect("home")
    else:
        form = EmailForm(instance=email, user=request.user)

    return render(
        request,
        "emails/compose.html",
        {"form": form, "email": email, "title": "Edit message"},
    )


@login_required
@require_POST
def email_delete(request, pk):
    email = get_object_or_404(visible_emails(request.user), pk=pk)
    if email.status == Email.Status.SENT:
        messages.error(request, "Sent messages cannot be deleted.")
        return redirect("home")
    email.delete()
    messages.success(request, "Message deleted.")
    return redirect(request.POST.get("next") or "home")


@login_required
@require_POST
def attachment_delete(request, pk):
    attachment = get_object_or_404(Attachment, pk=pk)
    email = attachment.email
    if not visible_emails(request.user).filter(pk=email.pk).exists():
        raise Http404
    if not email.is_editable:
        raise Http404
    attachment.delete()
    messages.success(request, "Attachment removed.")
    return redirect("email_edit", pk=email.pk)


@login_required
def attachment_download(request, pk):
    """Serve an attachment only to people who can see its message.

    Media is deliberately not served as public static files — these can be
    confidential documents.
    """
    attachment = get_object_or_404(Attachment, pk=pk)
    if not visible_emails(request.user).filter(pk=attachment.email_id).exists():
        raise Http404
    return FileResponse(
        attachment.file.open("rb"),
        as_attachment=True,
        filename=attachment.original_name,
    )


@login_required
def email_preview(request, pk):
    """Rendered preview — most useful for checking a mail merge."""
    email = get_object_or_404(visible_emails(request.user), pk=pk)
    return render(
        request,
        "emails/preview.html",
        {"email": email, "previews": email.merge_preview(limit=5)},
    )


# --- executive --------------------------------------------------------------
@executive_required
def exec_dashboard(request):
    drafts = visible_emails(request.user).drafts()
    now = timezone.now()
    credential = getattr(request.user, "google_credential", None)
    return render(
        request,
        "executive/dashboard.html",
        {
            "drafts": drafts,
            "due_count": drafts.filter(send_date__lte=now).count(),
            "now": now,
            "can_send": bool(credential and credential.can_send),
        },
    )


SCOPES = {
    "current": "Current messages",
    "future": "Future messages",
    "all": "All messages",
}


@executive_required
def exec_edit(request, scope):
    """Sequential editor over a filtered set of drafts.

    'current' is everything dated at or before now, 'future' is everything
    after now, 'all' applies no date filter.
    """
    if scope not in SCOPES:
        raise Http404

    now = timezone.now()
    drafts = visible_emails(request.user).drafts()
    if scope == "current":
        drafts = drafts.filter(send_date__lte=now)
    elif scope == "future":
        drafts = drafts.filter(send_date__gt=now)

    EmailFormSet = modelformset_factory(Email, form=EmailForm, extra=0)

    if request.method == "POST":
        formset = EmailFormSet(request.POST, request.FILES, queryset=drafts)
        if formset.is_valid():
            formset.save()
            messages.success(request, "All changes saved.")
            return redirect("exec_dashboard")
        messages.error(request, "Some messages could not be saved — see below.")
    else:
        formset = EmailFormSet(queryset=drafts)

    return render(
        request,
        "executive/edit.html",
        {
            "formset": formset,
            "scope": scope,
            "scope_label": SCOPES[scope],
            "count": drafts.count(),
        },
    )


@executive_required
@require_POST
def exec_send_one(request, pk):
    email = get_object_or_404(visible_emails(request.user).drafts(), pk=pk)
    try:
        delivered = send_email(email, triggered_by=request.user)
    except SendError as exc:
        messages.error(request, f"Could not send “{email.summary_subject}”: {exc}")
    else:
        suffix = f" to {delivered} recipients" if email.is_mail_merge else ""
        messages.success(request, f"Sent “{email.summary_subject}”{suffix}.")
    return redirect("exec_dashboard")


@executive_required
@require_POST
def exec_send_current(request):
    """Send every draft dated at or before today, per the spec's example."""
    due = list(visible_emails(request.user).current())
    if not due:
        messages.info(request, "There are no messages due to send.")
        return redirect("exec_dashboard")

    sent, failed = 0, 0
    for email in due:
        try:
            send_email(email, triggered_by=request.user)
        except SendError as exc:
            failed += 1
            messages.error(
                request, f"Could not send “{email.summary_subject}”: {exc}"
            )
        else:
            sent += 1

    if sent:
        messages.success(
            request, f"Sent {sent} message{'s' if sent != 1 else ''}."
        )
    if failed:
        messages.warning(
            request, f"{failed} message{'s' if failed != 1 else ''} failed."
        )
    return redirect("exec_dashboard")


@executive_required
def exec_sent(request):
    emails = Email.objects.filter(
        executive=request.user, status__in=[Email.Status.SENT, Email.Status.FAILED]
    ).order_by("-sent_at", "-updated_at")
    return render(request, "executive/sent.html", {"emails": emails})


@executive_required
def exec_signature(request):
    if request.method == "POST":
        form = SignatureForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Signature updated.")
            return redirect("exec_signature")
    else:
        form = SignatureForm(instance=request.user)
    return render(request, "executive/signature.html", {"form": form})


@executive_required
def exec_workers(request):
    """Issue and manage the assistant (worker) accounts for this mailbox."""
    if request.method == "POST":
        form = WorkerCreateForm(request.POST)
        if form.is_valid():
            worker = User(
                username=form.cleaned_data["username"],
                display_name=form.cleaned_data["display_name"],
                role=User.Role.ASSISTANT,
                executive=request.user,
            )
            worker.set_password(form.cleaned_data["password"])
            worker.save()
            messages.success(
                request,
                f"Worker account “{worker.username}” created. Share the username "
                "and password with your assistant.",
            )
            return redirect("exec_workers")
    else:
        form = WorkerCreateForm()

    return render(
        request,
        "executive/workers.html",
        {"form": form, "workers": request.user.assistants.order_by("username")},
    )


@executive_required
def exec_worker_password(request, pk):
    worker = get_object_or_404(request.user.assistants, pk=pk)
    if request.method == "POST":
        form = WorkerPasswordForm(request.POST)
        if form.is_valid():
            worker.set_password(form.cleaned_data["password"])
            worker.save()
            messages.success(request, f"Password updated for “{worker.username}”.")
            return redirect("exec_workers")
    else:
        form = WorkerPasswordForm()
    return render(
        request,
        "executive/worker_password.html",
        {"form": form, "worker": worker},
    )


@executive_required
@require_POST
def exec_worker_delete(request, pk):
    worker = get_object_or_404(request.user.assistants, pk=pk)
    username = worker.username
    worker.delete()
    messages.success(request, f"Worker account “{username}” removed.")
    return redirect("exec_workers")


@executive_required
def exec_logs(request, pk):
    email = get_object_or_404(Email.objects.filter(executive=request.user), pk=pk)
    return render(
        request,
        "executive/logs.html",
        {"email": email, "logs": email.logs.all()},
    )
