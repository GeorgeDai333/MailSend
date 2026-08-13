from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import Attachment, Email, GoogleCredential, SendLog, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "email", "role", "executive", "is_staff")
    list_filter = ("role", "is_staff", "is_superuser")
    fieldsets = BaseUserAdmin.fieldsets + (
        ("MailSend", {"fields": ("role", "executive", "display_name", "signature")}),
    )


class AttachmentInline(admin.TabularInline):
    model = Attachment
    extra = 0


@admin.register(Email)
class EmailAdmin(admin.ModelAdmin):
    list_display = ("subject", "executive", "created_by", "send_date", "status")
    list_filter = ("status", "is_mail_merge")
    search_fields = ("subject", "to", "body")
    inlines = [AttachmentInline]


@admin.register(SendLog)
class SendLogAdmin(admin.ModelAdmin):
    list_display = ("recipient", "subject", "succeeded", "created_at")
    list_filter = ("succeeded",)


admin.site.register(GoogleCredential)
admin.site.site_header = "MailSend administration"
