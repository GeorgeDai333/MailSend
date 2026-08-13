"""Attachment safety checks.

Gmail refuses to attach certain file types because they can execute on the
recipient's machine. The Gmail *API* does not enforce that list, so a message
sent through MailSend can carry a file the Gmail web interface would have
rejected — which is why this check exists here.

The blocklist mirrors Gmail's exactly. Types Gmail permits are permitted here,
including scripting languages such as .py, .sh and .rb.
"""

import os
import zipfile

# Gmail's published list of blocked extensions, reproduced verbatim.
# https://support.google.com/mail/answer/6590
GMAIL_BLOCKED_EXTENSIONS = frozenset(
    {
        ".ade", ".adp", ".apk", ".appx", ".appxbundle", ".bat", ".cab", ".chm",
        ".cmd", ".com", ".cpl", ".diagcab", ".diagcfg", ".diagpkg", ".dll",
        ".dmg", ".ex", ".ex_", ".exe", ".hta", ".img", ".ins", ".iso", ".isp",
        ".jar", ".jnlp", ".js", ".jse", ".lib", ".lnk", ".mde", ".mjs", ".msc",
        ".msi", ".msix", ".msixbundle", ".msp", ".mst", ".nsh", ".pif", ".ps1",
        ".scr", ".sct", ".shb", ".sys", ".vb", ".vbe", ".vbs", ".vhd", ".vxd",
        ".wsc", ".wsf", ".wsh", ".xll",
    }
)

BLOCKED_EXTENSIONS = GMAIL_BLOCKED_EXTENSIONS

# Gmail blocks these types inside archives too, so we look one level deep.
ARCHIVE_EXTENSIONS = frozenset({".zip"})


def extension_of(filename):
    return os.path.splitext(filename or "")[1].lower()


def _blocked_names_in_zip(uploaded_file):
    """Return blocked entry names inside a zip, or [] if it is unreadable.

    An unreadable archive is left to the normal attachment path rather than
    rejected — a corrupt zip is a user problem, not a security one.
    """
    try:
        uploaded_file.seek(0)
        with zipfile.ZipFile(uploaded_file) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError, ValueError):
        return []
    finally:
        try:
            uploaded_file.seek(0)
        except (OSError, ValueError):
            pass

    return [
        name
        for name in names
        if not name.endswith("/") and extension_of(name) in BLOCKED_EXTENSIONS
    ]


def rejection_reason(uploaded_file):
    """Why this upload may not be attached, or None if it is fine."""
    name = getattr(uploaded_file, "name", "") or ""
    extension = extension_of(name)

    if extension in BLOCKED_EXTENSIONS:
        return (
            f"“{name}” is a {extension} file, which Gmail blocks because it can "
            "run code on the recipient's computer. Put it in a shared drive and "
            "send the link instead."
        )

    if extension in ARCHIVE_EXTENSIONS:
        blocked = _blocked_names_in_zip(uploaded_file)
        if blocked:
            listed = ", ".join(sorted(blocked)[:3])
            more = "" if len(blocked) <= 3 else f" (+{len(blocked) - 3} more)"
            return (
                f"“{name}” contains file types Gmail blocks inside archives: "
                f"{listed}{more}."
            )

    return None
