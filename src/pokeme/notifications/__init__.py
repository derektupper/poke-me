from abc import ABC, abstractmethod


class NotificationChannel(ABC):
    """Base class for notification channels (desktop, SMS, email, etc.)."""

    @abstractmethod
    def notify(self, question: str, agent: str | None, url: str) -> None:
        """Send a notification that an agent needs human input.

        Args:
            question: The question the agent is asking.
            agent: Optional name of the agent asking.
            url: URL where the user can respond.
        """
