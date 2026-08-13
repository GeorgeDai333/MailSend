"""Attachment safety checks.

Gmail refuses to attach certain file types because they can execute on the
recipient's machine. The Gmail *API* does not enforce that list, so a message
sent through MailSend can carry a file the Gmail web interface would have
rejected — which is why this check exists here.
"""

import os
import zipfile

# Gmail's published list of blocked extensions.
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

# Gmail's list predates a lot of scripting and misses these — a .py or .sh is
# every bit as executable as a .bat. An executive forwarding a script they were
# handed is exactly the risk this tool should not add.
EXTRA_BLOCKED_EXTENSIONS = frozenset(
    {
        ".py", ".pyc", ".pyo", ".pyw", ".pyz",
        ".sh", ".bash", ".zsh", ".ksh", ".csh", ".fish",
        ".rb", ".pl", ".php", ".lua", ".tcl", ".r",
        ".ahk", ".applescript", ".command", ".osa", ".scpt",
        ".jsx", ".ts", ".vbscript", ".workflow", ".action",
        ".desktop", ".run", ".bin", ".elf", ".out",
        ".reg", ".inf", ".job", ".scf", ".url", ".website",
        ".ps1xml", ".psc1", ".psd1", ".psm1", ".pssc",
        ".gadget", ".hlp", ".its", ".jnt", ".mad", ".maf", ".mag", ".mam",
        ".maq", ".mar", ".mas", ".mat", ".mau", ".mav", ".maw", ".mcf",
        ".msh", ".msh1", ".msh2", ".mshxml", ".msh1xml", ".msh2xml",
        ".plg", ".prf", ".prg", ".pst", ".shs", ".theme", ".vsmacros",
        ".vsw", ".ws", ".xnk",
    }
)

BLOCKED_EXTENSIONS = GMAIL_BLOCKED_EXTENSIONS | EXTRA_BLOCKED_EXTENSIONS

# Archives are inspected one level deep, as Gmail does: a blocked file inside
# a .zip is still a blocked file.
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

    if not extension:
        # No extension means the recipient's mail client guesses how to open
        # it, which is the same hazard by a quieter route.
        return (
            f"“{name}” has no file extension, so it cannot be checked for "
            "safety. Rename it with its proper extension and try again."
        )

    if extension in BLOCKED_EXTENSIONS:
        return (
            f"“{name}” is a {extension} file, which can run code on the "
            "recipient's computer. Gmail blocks these too. Put it in a shared "
            "drive and send the link instead."
        )

    if extension in ARCHIVE_EXTENSIONS:
        blocked = _blocked_names_in_zip(uploaded_file)
        if blocked:
            listed = ", ".join(sorted(blocked)[:3])
            more = "" if len(blocked) <= 3 else f" (+{len(blocked) - 3} more)"
            return (
                f"“{name}” contains files that can run code on the recipient's "
                f"computer: {listed}{more}. Gmail blocks these inside archives "
                "too."
            )

    return None
