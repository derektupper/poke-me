import platform
import subprocess
import sys

from . import NotificationChannel


class DesktopChannel(NotificationChannel):
    """Send native OS desktop notifications via subprocess calls."""

    def notify(self, question: str, agent: str | None, url: str) -> None:
        title = "pokeme"
        if agent:
            title = f"pokeme: {agent}"

        body = question
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
        # Use PowerShell with Windows built-in toast notifications
        ps_script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null

$template = @"
<toast activationType="protocol" launch="{url}">
    <visual>
        <binding template="ToastGeneric">
            <text>{title}</text>
            <text>{body}</text>
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
        script = (
            f'display notification "{_escape_applescript(body)}" '
            f'with title "{_escape_applescript(title)}"'
        )
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=10,
        )

    def _notify_linux(self, title: str, body: str) -> None:
        subprocess.run(
            ["notify-send", title, body, "--app-name=pokeme"],
            capture_output=True,
            timeout=10,
        )

    def _fallback(self, title: str, body: str, url: str) -> None:
        print(f"\n*** {title}: {body}", file=sys.stderr)
        print(f"*** Respond at: {url}", file=sys.stderr)


def _escape_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
