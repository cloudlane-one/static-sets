"""Demo module for producing hello world output with additional info."""

from dataclasses import dataclass
from datetime import datetime

from structlog.stdlib import get_logger

logger = get_logger()


@dataclass
class Greeter:
    """Class for saying hello."""

    name: str
    """Name of the greeter."""

    correspondent: str = "world"
    """Name of the one who is greeted."""

    tell_time: bool = True
    """Tell the current time along with the greeting."""

    def say_hello(self) -> str:
        """Return a greet with a hello and optional info about time or weather."""
        greeting = f"Hello {self.correspondent}, {self.name} here!"

        if self.tell_time:
            greeting += f" The current time is {datetime.now().strftime('%H:%M:%S')}."

        logger.info(
            "Saying hello in logs too for good measure", greeting=greeting, tell_time=self.tell_time
        )

        return greeting
