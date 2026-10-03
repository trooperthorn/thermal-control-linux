"""systemd readiness and watchdog notifications using only the standard library.

systemd passes the path of a datagram socket in NOTIFY_SOCKET. A path starting with an
at sign is an abstract socket, which Linux spells with a leading null byte. When the
variable is unset (a run outside systemd, or any test) every call does nothing.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import Mapping

log = logging.getLogger("thermalctl.notify")


class Notifier:
    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        self.address = env.get("NOTIFY_SOCKET") or None

    @property
    def enabled(self) -> bool:
        return self.address is not None

    def send(self, message: str) -> bool:
        """Send one notification. Returns False when disabled or when sending failed."""
        if self.address is None:
            return False
        address = self.address
        if address.startswith("@"):
            address = "\0" + address[1:]
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.sendto(message.encode("utf-8"), address)
        except (OSError, AttributeError) as exc:
            log.error("sd_notify %r failed: %s", message, exc)
            return False
        return True

    def ready(self) -> bool:
        return self.send("READY=1")

    def watchdog(self) -> bool:
        return self.send("WATCHDOG=1")

    def stopping(self) -> bool:
        return self.send("STOPPING=1")
