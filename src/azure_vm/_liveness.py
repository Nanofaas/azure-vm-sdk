"""Close an SSH transport whose peer stops answering.

paramiko's ``Transport.set_keepalive`` sends ignore messages and never waits
for an answer, so a peer lost behind a NAT or a stalled TCP flow goes
unnoticed: reads, exit-status waits and SFTP calls then block until the kernel
gives up on the connection, which once took 55 minutes. This watchdog does
what OpenSSH's ``ServerAliveInterval`` and ``ServerAliveCountMax`` do: it asks
the peer for a reply every interval and closes the transport when a request
stays unanswered for ``interval * count_max`` seconds. Closing the transport
wakes every read, write and SFTP call blocked on it.
"""

from __future__ import annotations

import logging
import threading

import paramiko

_LOGGER = logging.getLogger(__name__)

# OpenSSH servers answer this request (with a failure) and do nothing else.
KEEPALIVE_REQUEST = "keepalive@openssh.com"


class Liveness:
    """Probe one transport in the background until stopped or expired."""

    def __init__(
        self,
        transport: paramiko.Transport,
        *,
        interval: float,
        count_max: int,
        label: str,
    ) -> None:
        """Watch ``transport``; nothing runs until ``start``."""
        self._transport = transport
        self._interval = interval
        self.deadline = interval * count_max
        self._label = label
        self._stop = threading.Event()
        self._expired = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name=f"ssh-liveness-{label}", daemon=True
        )

    @property
    def expired(self) -> bool:
        """Whether the watchdog closed the transport for an unanswered probe."""
        return self._expired.is_set()

    def start(self) -> None:
        """Begin probing in the background."""
        self._thread.start()

    def stop(self) -> None:
        """Stop probing; a pending probe ends when its transport closes."""
        self._stop.set()

    def _expire(self) -> None:
        if self._stop.is_set():
            return
        self._expired.set()
        _LOGGER.warning(
            "SSH session to %s answered no keepalive within %gs; closing it",
            self._label,
            self.deadline,
        )
        self._transport.close()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            if not self._transport.is_active():
                return
            timer = threading.Timer(self.deadline, self._expire)
            timer.daemon = True
            timer.start()
            try:
                self._transport.global_request(KEEPALIVE_REQUEST, wait=True)
            except (EOFError, OSError, paramiko.SSHException):
                # A probe that cannot even be sent on a live transport (a key
                # exchange that never completes, for one) is a stall as well.
                if self._transport.is_active():
                    self._expire()
                return
            finally:
                timer.cancel()
            if self._expired.is_set():
                return
