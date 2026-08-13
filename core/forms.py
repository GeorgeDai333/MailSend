from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import Email, User

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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["cc"].label = "CC"
        self.fields["bcc"].label = "BCC"
        if not self.instance.pk:
            self.fields["send_date"].initial = timezone.localtime()

    def clean(self):
        cleaned = super().clean()
        is_merge = cleaned.get("is_mail_merge")
        csv_file = cleaned.get("merge_csv")

        if is_merge:
            # An existing instance may already hold a CSV from a prior save.
            if not csv_file and not self.instance.merge_csv:
                raise ValidationError(
                    "Upload a CSV of recipients to use a mail merge."
                )
        else:
            if not cleaned.get("to") and not cleaned.get("cc") and not cleaned.get("bcc"):
                raise ValidationError(
                    "Add at least one recipient in To, CC or BCC."
                )
        return cleaned


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
