"""Public OKX ticker transport; all network work stays outside the GUI thread."""
import asyncio
import json
import threading
import time
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QThread, Signal
from okx.websocket.WsPublicAsync import WsPublicAsync

PUBLIC_URL = "wss://ws.okx.com/ws/v5/public"


def parse_ticker(data):
    """Preserve price precision and calculate change since UTC midnight."""
    try:
        last = Decimal(data["last"])
        instrument = data["instId"]
        if not last.is_finite() or last <= 0:
            return None
    except (KeyError, TypeError, InvalidOperation):
        return None
    change = None
    try:
        opened = Decimal(data["sodUtc0"])
        if opened.is_finite() and opened > 0:
            change = (last / opened - 1) * 100
    except (KeyError, TypeError, InvalidOperation, ZeroDivisionError):
        pass  # A missing UTC baseline must not suppress live prices or voice alerts.
    return instrument, format(last, "f"), change


class MarketWorker(QThread):
    ticker = Signal(str, str, object)
    status = Signal(str)
    subscription_error = Signal(str, str)

    def __init__(self, instruments, parent=None):
        super().__init__(parent)
        self._lock = threading.Lock()
        self._instruments = set(instruments)
        self._loop = None
        self._task = None

    def set_instruments(self, instruments):
        with self._lock:
            self._instruments = set(instruments)

    def stop(self):
        self.requestInterruption()
        with self._lock:
            if self._loop is not None and self._task is not None:
                try:
                    self._loop.call_soon_threadsafe(self._task.cancel)
                except RuntimeError:
                    pass  # Thread has already completed its event loop.

    def run(self):
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            pass
        finally:
            with self._lock:
                self._loop = self._task = None

    async def _run(self):
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
        while not self.isInterruptionRequested():
            client = WsPublicAsync(PUBLIC_URL)
            try:
                self.status.emit("正在连接 OKX…")
                await asyncio.wait_for(client.connect(), 10)
                if client.websocket is None:
                    raise ConnectionError("无法连接 OKX 公共行情服务")
                self.status.emit("行情已连接 · 等待报价")
                subscribed = set()
                rejected = set()
                last_message = time.monotonic()
                ping_sent = False
                while not self.isInterruptionRequested():
                    with self._lock:
                        desired = self._instruments.copy()
                    rejected.intersection_update(desired)
                    added, removed = desired - subscribed - rejected, subscribed - desired
                    if removed:
                        await client.unsubscribe(self._args(removed), self._message)
                    if added:
                        await client.subscribe(self._args(added), self._message)
                    subscribed = (subscribed - removed) | added
                    try:
                        message = await asyncio.wait_for(client.websocket.recv(), 0.5)
                    except asyncio.TimeoutError:
                        idle = time.monotonic() - last_message
                        if idle > 30:
                            raise TimeoutError("行情心跳超时")
                        if idle > 20 and not ping_sent:
                            await client.websocket.send("ping")
                            ping_sent = True
                        continue
                    last_message = time.monotonic()
                    ping_sent = False
                    failed = self._message(message)
                    if failed:
                        rejected.add(failed)
                        subscribed.discard(failed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status.emit(f"连接中断，3 秒后重试 · {str(exc)[:100]}")
                await asyncio.sleep(3)
            finally:
                if client.websocket is not None:
                    try:
                        await asyncio.wait_for(client.stop(), 1)
                    except (Exception, asyncio.CancelledError):
                        client.websocket.transport.abort()

    @staticmethod
    def _args(instruments):
        return [{"channel": "tickers", "instId": inst} for inst in sorted(instruments)]

    def _message(self, message):
        if message == "pong":
            return None
        try:
            payload = json.loads(message)
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("event") == "error":
            inst = payload.get("arg", {}).get("instId", "")
            reason = payload.get("msg", "订阅失败")
            self.subscription_error.emit(inst, reason)
            return inst
        for data in payload.get("data", []):
            parsed = parse_ticker(data)
            if parsed:
                self.ticker.emit(*parsed)
        return None
