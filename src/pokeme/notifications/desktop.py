import platform
import re
import subprocess
import sys
from html import escape as html_escape

from . import NotificationChannel


def _sanitize(s: str) -> str:
    """Strip input to safe characters only. Prevents injection in all shells."""
    # Allow word chars, spaces, and minimal safe punctuation only.
    # Explicitly block: " ' ` $ & | ; < > \ { } [ ] # ~ ^ ! and newlines
    return re.sub(r"[^\w\s\-.,?:() ]", "", s)


class DesktopChannel(NotificationChannel):
    """Send native OS desktop notifications via subprocess calls."""

    def notify(self, question: str, agent: str | None, url: str) -> None:
        title = "pokeme"
        if agent:
            title = f"pokeme: {_sanitize(agent)}"

        body = _sanitize(question)
        if len(body) > 120:
            body = body[:117] + "..."

        system = platform.system()
        try:
            if system == "Windows":
                self._notify_windows(title, body, url)
            elif system == "Darwin":
                self._notify_macos(title, body)
            elif system == "Linux":
                self._notify_linux(title, body)
            else:
                self._fallback(title, body, url)
        except Exception:
            self._fallback(title, body, url)

    def _notify_windows(self, title: str, body: str, url: str) -> None:
        # XML-escape values for the toast template
        safe_title = html_escape(title, quote=True)
        safe_body = html_escape(body, quote=True)
        safe_url = html_escape(url, quote=True)

        ps_script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null

$template = @"
<toast activationType="protocol" launch="{safe_url}">
    <visual>
        <binding template="ToastGeneric">
            <text>{safe_title}</text>
            <text>{safe_body}</text>
        </binding>
    </visual>
    <audio silent="false"/>
</toast>
"@

$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("pokeme")
$notifier.Show($toast)
"""
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            timeout=10,
        )

    def _notify_macos(self, title: str, body: str) -> None:
        # Pass values via environment variables to avoid shell injection entirely
        script = (
            'display notification (system attribute "POKEME_BODY") '
            'with title (system attribute "POKEME_TITLE")'
        )
        import os
        env = {**os.environ, "POKEME_TITLE": title, "POKEME_BODY": body}
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=10,
            env=env,
        )

    def _notify_linux(self, title: str, body: str) -> None:
        # notify-send with list args is safe (no shell interpolation)
        subprocess.run(
            ["notify-send", title, body, "--app-name=pokeme"],
            capture_output=True,
            timeout=10,
        )

    def _fallback(self, title: str, body: str, url: str) -> None:
        print(f"\n*** {title}: {body}", file=sys.stderr)
        print(f"*** Respond at: {url}", file=sys.stderr)
