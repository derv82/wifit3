from __future__ import annotations

import html
import shutil
import subprocess
import sys


def _command(title: str, message: str) -> list[str] | None:
    notification_title = "wifit3"
    notification_message = f"{title}\n{message}"
    if sys.platform.startswith("linux"):
        executable = shutil.which("notify-send")
        return ([executable, "--app-name=wifit3", "--icon=network-wireless",
                 notification_title, notification_message] if executable else None)
    if sys.platform == "darwin":
        executable = shutil.which("osascript")
        if not executable:
            return None
        script = "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"
        return [executable, "-e", script, notification_title, notification_message]
    if sys.platform == "win32":
        executable = shutil.which("powershell") or shutil.which("pwsh")
        if not executable:
            return None
        title_xml = html.escape(notification_title, quote=True)
        message_xml = html.escape(notification_message, quote=True)
        script = (
            "$null=[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
            "ContentType=WindowsRuntime];"
            "$null=[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,"
            "ContentType=WindowsRuntime];"
            "$xml=New-Object Windows.Data.Xml.Dom.XmlDocument;"
            f"$xml.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text>{title_xml}</text>"
            f"<text>{message_xml}</text></binding></visual></toast>');"
            "$toast=New-Object Windows.UI.Notifications.ToastNotification $xml;"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('wifit3').Show($toast)"
        )
        return [executable, "-NoProfile", "-NonInteractive", "-Command", script]
    return None


def send_system_notification(title: str, message: str) -> bool:
    """Send a non-blocking desktop notification when the platform backend is available."""
    command = _command(title, message)
    if command is None:
        return False
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=sys.platform != "win32",
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                           if sys.platform == "win32" else 0),
        )
    except OSError:
        return False
    return True
