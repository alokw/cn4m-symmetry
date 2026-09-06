"""Push short status updates to the parent utility's status endpoint.

Equivalent to:

    curl -X POST http://<cn4m-host>:2640/suite/status \
         -d app=symmetry -d message="Synced 40 links" -d level=working
"""

import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("symmetry")

# How long to leave a failing endpoint alone, doubling per consecutive failure.
BACKOFF_START = 60
BACKOFF_MAX = 1800
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")


def container_hint(url):
    """Explain the usual reason a localhost status URL fails inside Docker."""
    if not os.path.exists("/.dockerenv"):
        return ""
    host = urllib.parse.urlparse(url).hostname or ""
    if host.lower() not in LOCAL_HOSTS:
        return ""
    return (
        " Note: inside a container, localhost is the container itself, not the "
        "machine running Docker. If cn4m runs on the host, use "
        "http://host.docker.internal:2640/suite/status; if it is another "
        "container, use its service name."
    )


class StatusPusher:
    """Fire-and-forget POSTs to cn4m's /suite/status.

    Every send happens on a daemon thread and swallows its errors: a status
    update is never worth delaying or interrupting a scan for, so an endpoint
    that is slow, down, or missing entirely changes nothing about mirroring.
    """

    def __init__(self, url, app="symmetry", level="working", timeout=5):
        self.url = url
        self.app = app
        self.level = level
        self.timeout = timeout
        self._failing = False
        self._failures = 0
        self._retry_at = 0.0
        self._lock = threading.Lock()
        self._sending = []

    @property
    def enabled(self):
        return bool(self.url)

    def _in_backoff(self):
        """True while a failing endpoint is being left alone."""
        with self._lock:
            if not self._failures or time.monotonic() >= self._retry_at:
                return False
            waiting = self._retry_at - time.monotonic()
        log.debug(
            "skipping status update: %s has failed %d time(s), next try in %.0fs",
            self.url, self._failures, waiting,
        )
        return True

    def send(self, message, level=None):
        if not self.enabled or self._in_backoff():
            return
        thread = threading.Thread(
            target=self._post, args=(message, level or self.level),
            name="status-push", daemon=True,
        )
        with self._lock:
            self._sending = [t for t in self._sending if t.is_alive()]
            self._sending.append(thread)
        thread.start()

    def drain(self, timeout=None):
        """Give in-flight updates a moment to finish before the process ends.

        Waits a little longer than a request is allowed to take, so a send that
        was about to succeed is not cut off at the finish line.

        The sending threads are daemons, so without this they are killed at
        interpreter exit - which would silently drop the update from a RUN_ONCE
        scan, and from the last scan before a shutdown signal.
        """
        with self._lock:
            outstanding = [t for t in self._sending if t.is_alive()]
        deadline = time.monotonic() + (self.timeout + 1 if timeout is None else timeout)
        for thread in outstanding:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.debug("giving up waiting on in-flight status updates")
                break
            thread.join(remaining)

    def _post(self, message, level):
        payload = urllib.parse.urlencode(
            {"app": self.app, "message": message, "level": level}
        ).encode("utf-8")
        request = urllib.request.Request(self.url, data=payload, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read(2048)  # drain so the connection can be reused
        except Exception as exc:  # never let a status update reach the scan loop
            if isinstance(exc, urllib.error.HTTPError):
                reason = "responded %s %s" % (exc.code, exc.reason)
            elif isinstance(exc, urllib.error.URLError):
                reason = "is unreachable (%s)" % exc.reason
            else:
                reason = "failed: %s" % exc
            with self._lock:
                self._failures += 1
                pause = min(BACKOFF_MAX, BACKOFF_START * (2 ** (self._failures - 1)))
                self._retry_at = time.monotonic() + pause
                failures = self._failures
            if not self._failing:
                self._failing = True
                log.warning(
                    "status endpoint %s %s - pausing updates for %ds, then retrying "
                    "with a longer gap each time.%s",
                    self.url, reason, pause, container_hint(self.url),
                )
            else:
                log.debug(
                    "status endpoint %s %s (failure %d, next try in %ds)",
                    self.url, reason, failures, pause,
                )
        else:
            with self._lock:
                self._failures = 0
                self._retry_at = 0.0
            if self._failing:
                self._failing = False
                log.info("status endpoint %s is reachable again", self.url)
            log.debug("pushed status: %s", message)


def link_message(stats):
    """The short line describing what a scan just linked."""
    text = "Discovered and linked %d new file%s" % (
        stats.linked, "" if stats.linked == 1 else "s"
    )
    if stats.relinked:
        text += ", refreshed %d" % stats.relinked
    return text
