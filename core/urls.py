from django.urls import path

from . import views

urlpatterns = [
    path("", views.landing, name="landing"),
    path("home/", views.home, name="home"),
    # auth
    path("login/", views.MailSendLoginView.as_view(), name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("google/start/", views.google_start, name="google_start"),
    path("google/callback/", views.google_callback, name="google_callback"),
    # shared message actions
    path("messages/new/", views.email_create, name="email_create"),
    path("messages/<int:pk>/edit/", views.email_edit, name="email_edit"),
    path("messages/<int:pk>/delete/", views.email_delete, name="email_delete"),
    path("messages/<int:pk>/preview/", views.email_preview, name="email_preview"),
    path(
        "attachments/<int:pk>/download/",
        views.attachment_download,
        name="attachment_download",
    ),
    path(
        "attachments/<int:pk>/delete/",
        views.attachment_delete,
        name="attachment_delete",
    ),
    # assistant
    path("outbox/", views.assistant_dashboard, name="assistant_dashboard"),
    path("outbox/sent/", views.assistant_sent, name="assistant_sent"),
    # executive
    path("dashboard/", views.exec_dashboard, name="exec_dashboard"),
    path("dashboard/edit/<str:scope>/", views.exec_edit, name="exec_edit"),
    path("dashboard/send/<int:pk>/", views.exec_send_one, name="exec_send_one"),
    path("dashboard/send-current/", views.exec_send_current, name="exec_send_current"),
    path("dashboard/sent/", views.exec_sent, name="exec_sent"),
    path("dashboard/logs/<int:pk>/", views.exec_logs, name="exec_logs"),
    path("signature/", views.exec_signature, name="exec_signature"),
    path("workers/", views.exec_workers, name="exec_workers"),
    path(
        "workers/<int:pk>/password/",
        views.exec_worker_password,
        name="exec_worker_password",
    ),
    path(
        "workers/<int:pk>/delete/",
        views.exec_worker_delete,
        name="exec_worker_delete",
    ),
]
