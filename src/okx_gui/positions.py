"""Read-only account positions using python-okx outside the GUI thread."""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import shlex
import threading

import httpx
from okx.Account import AccountAPI
from PySide6.QtCore import QThread, Signal

CREDENTIALS_PATH = Path.home() / ".config/okx-trader/credentials.env"


class PositionsError(ValueError):
    """A credential-safe error suitable for display."""


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    passphrase: str = field(repr=False)
    flag: str = "0"

    def redact(self, text):
        for secret in (self.api_key, self.secret_key, self.passphrase):
            if secret:
                text = text.replace(secret, "[已隐藏]")
        return text


def load_credentials(path=CREDENTIALS_PATH):
    """Parse plain dotenv assignments without executing shell commands."""
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        raise PositionsError("无法读取凭据文件，请检查 ~/.config/okx-trader/credentials.env。") from None
    values = {}
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        name, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name.strip()):
            raise PositionsError(f"凭据文件第 {number} 行格式不正确，应为 名称=值。")
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError:
            raise PositionsError(f"凭据文件第 {number} 行引号不匹配。") from None
        if len(parts) > 1:
            raise PositionsError(f"凭据文件第 {number} 行的值包含空格，请使用引号。")
        values[name.strip().replace("_", "").lower()] = parts[0] if parts else ""

    def required(label, *aliases):
        value = next((values[key] for key in aliases if values.get(key)), "")
        if not value:
            raise PositionsError(f"凭据文件缺少 {label}，请补充后点击刷新。")
        return value

    key = required("apiKey", "okxapikey", "apikey")
    secret = required("secretKey", "okxsecretkey", "okxapisecretkey", "apisecretkey", "secretkey")
    phrase = required("passphrase", "okxpassphrase", "passphrase")
    flag = values.get("okxflag", values.get("flag", "0"))
    if flag not in ("0", "1"):
        raise PositionsError("OKX_FLAG 必须为 0（实盘）或 1（模拟盘）。")
    return Credentials(key, secret, phrase, flag)


@dataclass(frozen=True)
class Position:
    instrument: str
    direction: str
    quantity: str
    average: str
    mark: str
    unrealized: str
    leverage: str
    margin_mode: str

    quantity_value: str = "--"
    quantity_currency: str = "张"
    margin: str = "--"
    margin_currency: str = ""
    unrealized_ratio: str = "--"
    maintenance_ratio: str = "--"
    liquidation: str = "--"
    price_decimals: int = 2

    @property
    def title(self):
        parts = self.instrument.split("-")
        return "".join(parts[:2]) + (" 永续" if self.instrument.endswith("-SWAP") else " " + "-".join(parts[2:])).rstrip()

    @property
    def sell(self):
        return self.direction in ("空", "净空")

    def cells(self):
        return (self.instrument, self.direction, self.quantity, self.average,
                self.mark, self.unrealized, self.leverage, self.margin_mode)


def parse_positions(data, instruments=None):
    if not isinstance(data, list):
        raise PositionsError("持仓响应格式异常，请稍后刷新。")
    instruments = instruments or {}
    positions = []
    for item in data:
        try:
            size = Decimal(item["pos"])
            instrument = item["instId"]
            if not size.is_finite() or not isinstance(instrument, str) or not instrument:
                raise ValueError
        except (KeyError, TypeError, InvalidOperation, ValueError):
            raise PositionsError("持仓响应包含无效数据，请稍后刷新。") from None
        if not size:
            continue
        kind = item.get("instType", "")
        side = item.get("posSide", "net")
        direction = {"long": "多", "short": "空"}.get(side)
        if direction is None:
            if kind == "MARGIN":
                currencies = instrument.split("-")
                currency = item.get("posCcy", "")
                direction = "多" if currency == currencies[0] else (
                    "空" if len(currencies) > 1 and currency == currencies[1] else "净持仓"
                )
            else:
                direction = "净多" if size > 0 else "净空"
        unit = item.get("posCcy", "") if kind == "MARGIN" else "张"
        quantity = f"{format(abs(size), 'f')} {unit}".strip()
        value = lambda key: str(item.get(key) or "--")
        unrealized = value("upl")
        if unrealized != "--" and item.get("ccy"):
            unrealized += f" {item['ccy']}"
        leverage = value("lever")
        if leverage != "--":
            leverage += "×"
        signed_size = -abs(size) if direction in ("空", "净空") else abs(size)
        quantity_currency = unit
        metadata = instruments.get(instrument, {})
        base = instrument.split("-")[0]
        if kind in ("SWAP", "FUTURES", "OPTION"):
            try:
                face = Decimal(metadata["ctVal"]) * Decimal(metadata.get("ctMult") or "1")
                if not face.is_finite() or face <= 0:
                    raise ValueError
                if metadata.get("ctValCcy") == base:
                    signed_size *= face
                    quantity_currency = base
                elif metadata.get("ctType") == "inverse":
                    mark = Decimal(item["markPx"])
                    if not mark.is_finite() or mark <= 0:
                        raise ValueError
                    signed_size = signed_size * face / mark
                    quantity_currency = base
            except (KeyError, InvalidOperation, ValueError, TypeError):
                pass  # Keep explicit contract units when metadata is unavailable.
        price_decimals = 2
        try:
            tick = Decimal(metadata["tickSz"])
            if tick.is_finite() and tick > 0:
                price_decimals = min(12, max(0, -tick.normalize().as_tuple().exponent))
        except (KeyError, InvalidOperation, ValueError, TypeError):
            pass
        margin_key = "imr" if item.get("mgnMode") == "cross" else "margin"
        positions.append(Position(
            instrument, direction, quantity, value("avgPx"), value("markPx"),
            unrealized, leverage,
            {"cross": "全仓", "isolated": "逐仓"}.get(item.get("mgnMode"), value("mgnMode")),
            format(signed_size, "f"), quantity_currency, value(margin_key),
            str(item.get("ccy") or "--"), value("uplRatio"), value("mgnRatio"), value("liqPx"), price_decimals,
        ))
    return positions


def fetch_positions(credentials, account_factory=AccountAPI, *, instrument_cache=None):
    try:
        with account_factory(
            api_key=credentials.api_key, api_secret_key=credentials.secret_key,
            passphrase=credentials.passphrase, flag=credentials.flag, debug=False,
        ) as account:
            account.timeout = httpx.Timeout(3.0)
            response = account.get_positions()
            instruments = instrument_cache if instrument_cache is not None else {}
            if isinstance(response, dict) and str(response.get("code")) == "0" and isinstance(response.get("data"), list):
                for item in response["data"]:
                    if not isinstance(item, dict) or item.get("pos") in ("0", "", None):
                        continue
                    instrument = item.get("instId")
                    if instrument in instruments or item.get("instType") not in ("SWAP", "FUTURES", "OPTION"):
                        continue
                    try:
                        metadata = account.get_instruments(instType=item["instType"], instId=instrument)
                        if str(metadata.get("code")) == "0":
                            match = next((row for row in metadata.get("data", []) if row.get("instId") == instrument), None)
                            if match:
                                instruments[instrument] = match
                    except Exception:
                        pass  # Positions remain usable with an explicit quantity in contracts.

    except httpx.HTTPError:
        raise PositionsError("持仓查询网络异常，请检查网络后刷新。") from None
    except Exception:
        raise PositionsError("持仓查询失败，请检查连接和凭据配置。") from None
    if not isinstance(response, dict) or "code" not in response:
        raise PositionsError("持仓响应格式异常，请稍后刷新。")
    if str(response["code"]) != "0":
        message = credentials.redact(f"OKX 查询失败（{response['code']}）：{response.get('msg', '请求被拒绝')}")
        raise PositionsError(message[:240])
    return parse_positions(response.get("data"), instruments)


class PositionsWorker(QThread):
    loading = Signal()
    updated = Signal(object, str)
    error = Signal(str)

    def __init__(self, parent=None, *, credentials_path=CREDENTIALS_PATH, interval=5,
                 fetcher=None):
        super().__init__(parent)
        self.credentials_path = credentials_path
        self.interval = interval
        self._instruments = {}
        self.fetcher = fetcher or (lambda credentials: fetch_positions(credentials, instrument_cache=self._instruments))
        self._wake = threading.Event()

    def refresh(self):
        self._wake.set()

    def stop(self):
        self.requestInterruption()
        self._wake.set()

    def run(self):
        while not self.isInterruptionRequested():
            self._wake.clear()
            self.loading.emit()
            try:
                credentials = load_credentials(self.credentials_path)
                positions = self.fetcher(credentials)
                if not self.isInterruptionRequested():
                    self.updated.emit(positions, credentials.flag)
            except PositionsError as exc:
                if not self.isInterruptionRequested():
                    self.error.emit(str(exc))
            except Exception:
                if not self.isInterruptionRequested():
                    self.error.emit("持仓读取失败，请检查配置后刷新。")
            if not self.isInterruptionRequested():
                self._wake.wait(self.interval)
