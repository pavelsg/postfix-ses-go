"""Capture ordinary SMTP deliveries inside the disposable Postfix container."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from queue import Queue

from aiosmtpd.controller import Controller
from aiosmtpd.smtp import Envelope, Session, SMTP


@dataclass(frozen=True)
class Delivery:
    """Envelope and DATA bytes received from the real Postfix SMTP transport."""

    sender: str
    recipients: tuple[str, ...]
    raw: bytes


class Sink:
    """Accept SMTP transactions and expose complete deliveries in arrival order."""

    # Each scenario runs alone in a network-isolated container.
    port = 8025

    def __init__(self) -> None:
        """Create the thread-safe queue shared with the test driver."""
        self.messages: Queue[Delivery] = Queue()

    async def handle_DATA(self, server: SMTP, session: Session, envelope: Envelope) -> str:
        """Record DATA after SMTP dot unstuffing and acknowledge the transaction."""
        assert envelope.mail_from is not None and isinstance(envelope.original_content, bytes)
        # aiosmtpd spells the null reverse-path as "<>"; expose the same
        # empty sender as the Postfix pipe contract.
        sender = "" if envelope.mail_from == "<>" else envelope.mail_from
        self.messages.put(Delivery(sender, tuple(envelope.rcpt_tos), envelope.original_content))
        return "250 2.0.0 captured"


@contextmanager
def smtp_sink() -> Iterator[Sink]:
    """Serve SMTP on loopback while Postfix exercises its ordinary delivery path."""
    sink = Sink()
    controller = Controller(sink, hostname="127.0.0.1", port=sink.port)
    controller.start()
    try:
        yield sink
    finally:
        controller.stop()
