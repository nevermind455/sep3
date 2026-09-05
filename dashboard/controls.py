"""Non-blocking keyboard controls for the interactive dashboard."""
from __future__ import annotations

import sys
import threading
import time


class Keys:
    """Read single keys without blocking the bot's asyncio event loop."""

    def __init__(self) -> None:
        self.q: list[str] = []
        self._q_lock = threading.Lock()
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self._restore = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self._t is not None and self._t.is_alive():
            return
        if not sys.stdin.isatty():
            return
        self._stop.clear()
        if sys.platform == "win32":
            self._t = threading.Thread(target=self._win, daemon=True)
        else:
            try:
                import termios
                import tty
                fd = sys.stdin.fileno()
                self._restore = (fd, termios.tcgetattr(fd))
                tty.setcbreak(fd)
            except Exception as exc:
                self.last_error = (
                    f"keyboard setup failed: {type(exc).__name__}: {exc}"
                )[:200]
                self._restore_terminal()
                return
            self._t = threading.Thread(target=self._posix, daemon=True)
        try:
            self._t.start()
        except Exception as exc:
            self._t = None
            self.last_error = (
                f"keyboard thread failed: {type(exc).__name__}: {exc}"
            )[:200]
            self._restore_terminal()

    def _posix(self) -> None:
        import select
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.2)
                if ready:
                    with self._q_lock:
                        self.q.append(sys.stdin.read(1))
            except Exception as exc:
                self.last_error = (
                    f"keyboard reader failed: {type(exc).__name__}: {exc}"
                )[:200]
                return

    def _win(self) -> None:
        import msvcrt
        while not self._stop.is_set():
            try:
                if msvcrt.kbhit():
                    with self._q_lock:
                        self.q.append(msvcrt.getwch())
                else:
                    time.sleep(0.05)
            except Exception as exc:
                self.last_error = (
                    f"keyboard reader failed: {type(exc).__name__}: {exc}"
                )[:200]
                return

    def pop(self) -> list[str]:
        with self._q_lock:
            out, self.q = self.q, []
            return out

    def stop(self) -> None:
        self._stop.set()
        thread = self._t
        self._t = None
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.0)
            if thread.is_alive() and self.last_error is None:
                self.last_error = "keyboard reader did not stop within 1s"
        self._restore_terminal()

    def _restore_terminal(self) -> None:
        if not self._restore:
            return
        try:
            import termios
            fd, old = self._restore
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception as exc:
            detail = f"terminal restore failed: {type(exc).__name__}"
            self.last_error = f"{self.last_error}; {detail}" if self.last_error else detail
        finally:
            self._restore = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False
