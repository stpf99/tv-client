"""
GTK dziala na petli GLib, HTSP klient na petli asyncio. Ten modul uruchamia
asyncio we wlasnym watku i dostarcza bezpieczne mostki w obie strony:

  - `bridge.call(coro_func, *a, **kw)` -> odpala korutyne w watku asyncio
    i zwraca `concurrent.futures.Future`; wynik mozna odebrac w GTK przez
    `GLib.idle_add`.
  - `bridge.emit_to_gtk(callback, *args)` -> wywoluje `callback(*args)` w
    petli GLib (do uzycia z callbackow HTSP, ktore strzelaja z watku asyncio).
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future as ConcurrentFuture
from typing import Any, Callable

from gi.repository import GLib


class AsyncBridge:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, name="htsp-asyncio", daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def call(self, coro) -> ConcurrentFuture:
        """Odpala korutyne (juz utworzona, np. client.hello()) w watku asyncio."""
        assert self._loop is not None, "AsyncBridge.start() nie zostalo wywolane"
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def call_with_callback(self, coro, on_success: Callable[[Any], None],
                            on_error: Callable[[Exception], None] | None = None) -> None:
        """
        Odpala korutyne w watku asyncio i po zakonczeniu wywoluje callback
        w petli GLib (bezpiecznie dla GTK).
        """
        fut = self.call(coro)

        def _done(f: ConcurrentFuture) -> None:
            try:
                result = f.result()
            except Exception as exc:  # noqa: BLE001
                if on_error:
                    GLib.idle_add(on_error, exc)
                return
            GLib.idle_add(on_success, result)

        fut.add_done_callback(_done)

    @staticmethod
    def emit_to_gtk(callback: Callable, *args) -> None:
        """Wywoluje callback bezpiecznie w petli GLib z watku asyncio."""
        GLib.idle_add(callback, *args)


# Pojedyncza globalna instancja - jedna petla asyncio na cala aplikacje
bridge = AsyncBridge()
