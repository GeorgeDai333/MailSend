from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import Email, User, recipients_from_csv

# HTML datetime-local inputs post this format; keep it first so re-rendering a
# bound form round-trips cleanly.
DATETIME_FORMATS = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]


class DateTimeLocalField(forms.DateTimeField):
    widget = forms.DateTimeInput(
        attrs={"type": "datetime-local", "class": "form-control"},
        format="%Y-%m-%dT%H:%M",
    )

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("input_formats", DATETIME_FORMATS)
        super().__init__(*args, **kwargs)


class EmailForm(forms.ModelForm):
    """Compose / edit form used by both the assistant and the executive."""

    send_date = DateTimeLocalField(
        label="Send date",
        help_text="The date this message is meant to go out.",
    )

    class Meta:
        model = Email
        fields = [
            "to",
            "cc",
            "bcc",
            "subject",
            "body",
            "send_date",
            "auto_send",
            "is_mail_merge",
            "merge_csv",
        ]
        widgets = {
            "to": forms.Textarea(
                attrs={
                    "rows": 1,
                    "class": "form-control",
                    "placeholder": "name@example.com, another@example.com",
                }
            ),
            "cc": forms.Textarea(attrs={"rows": 1, "class": "form-control"}),
            "bcc": forms.Textarea(attrs={"rows": 1, "class": "form-control"}),
            "subject": forms.TextInput(attrs={"class": "form-control"}),
            "body": forms.Textarea(attrs={"rows": 14, "class": "form-control body-box"}),
            "auto_send": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_mail_merge": forms.CheckboxInput(
                attrs={"class": "form-check-input", "id": "id_is_mail_merge"}
            ),
            "merge_csv": forms.ClearableFileInput(
                attrs={"class": "form-control", "accept": ".csv"}
            ),
        }
        labels = {
            "auto_send": "Send automatically once the send date passes",
            "is_mail_merge": "This is a mail merge",
            "merge_csv": "Recipient CSV",
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Set by clean() so the view can tell the user what just happened.
        self.csv_was_cleared = False
        self.recipients_loaded = 0

        # Only the executive decides whether anything goes out unattended.
        # Removing the field — rather than hiding it — means an assistant
        # posting auto_send by hand cannot set it either. An existing value
        # set by the executive survives an assistant's edit untouched.
        if user is not None and user.is_assistant:
            self.fields.pop("auto_send", None)

        self.fields["cc"].label = "CC"
        self.fields["bcc"].label = "BCC"
        if not self.instance.pk:
            self.fields["send_date"].initial = timezone.localtime()

    def clean(self):
        cleaned = super().clean()
        csv_value = cleaned.get("merge_csv")

        # A FileField with ClearableFileInput reports three different states:
        # False means the Clear box was ticked, a file object means a new
        # upload, and None means "leave whatever is already saved alone".
        cleared = csv_value is False
        uploaded = bool(csv_value) and csv_value is not False

        if cleared:
            # A mail merge with no recipient list is not a mail merge, and
            # leaving the flag set would strand the draft in a state that
            # looks merged but sends to nobody.
            cleaned["is_mail_merge"] = False
            cleaned["to"] = ""
            self.csv_was_cleared = True
        elif uploaded:
            # A new list replaces the old recipients outright.
            addresses = self._addresses_from_upload(csv_value)
            if not addresses:
                self.add_error(
                    "merge_csv",
                    "No recipients found — the CSV needs a header row with an "
                    "“email” column.",
                )
            else:
                cleaned["to"] = ", ".join(addresses)
                cleaned["is_mail_merge"] = True
                self.recipients_loaded = len(addresses)

        is_merge = cleaned.get("is_mail_merge")
        if is_merge:
            if not uploaded and not self.instance.merge_csv:
                raise ValidationError(
                    "Upload a CSV of recipients to use a mail merge."
                )
        elif not cleared:
            if not cleaned.get("to") and not cleaned.get("cc") and not cleaned.get("bcc"):
                raise ValidationError(
                    "Add at least one recipient in To, CC or BCC."
                )
        return cleaned

    @staticmethod
    def _addresses_from_upload(upload):
        """Read recipient addresses out of a freshly uploaded CSV.

        The stream is rewound afterwards so the file still saves intact.
        """
        try:
            upload.seek(0)
            raw = upload.read()
        finally:
            upload.seek(0)
        return recipients_from_csv(raw)


class SignatureForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["signature"]
        widgets = {
            "signature": forms.Textarea(attrs={"rows": 8, "class": "form-control"})
        }
        labels = {"signature": "Signature"}
        help_texts = {
            "signature": "Appended to the bottom of every message sent from this account."
        }


class WorkerCreateForm(forms.Form):
    """An executive issuing credentials for their assistant."""

    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control"}),
        help_text="The assistant signs in with this.",
    )
    display_name = forms.CharField(
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
        min_length=8,
    )

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("That username is already taken.")
        return username


class WorkerPasswordForm(forms.Form):
    password = forms.CharField(
        label="New password",
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
        min_length=8,
    )


class StyledLoginForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update(
            {"class": "form-control", "autofocus": True}
        )
        self.fields["password"].widget.attrs.update({"class": "form-control"})
