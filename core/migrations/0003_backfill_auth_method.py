from django.db import migrations


def set_executives_to_google(apps, schema_editor):
    """Executives have always signed in with Google.

    The new auth_method column defaults to 'password', which is correct for
    existing workers but wrong for every executive, so correct them here.
    """
    User = apps.get_model("core", "User")
    User.objects.filter(role="EXEC").update(auth_method="google")


def reverse(apps, schema_editor):
    User = apps.get_model("core", "User")
    User.objects.filter(role="EXEC").update(auth_method="password")


class Migration(migrations.Migration):
    dependencies = [("core", "0002_user_auth_method")]

    operations = [migrations.RunPython(set_executives_to_google, reverse)]
