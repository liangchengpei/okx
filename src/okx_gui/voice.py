"""Decimal price ladder and nonblocking native speech queue."""
from collections import deque
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

from PySide6.QtCore import QObject, QLocale, QTimer, Signal
from PySide6.QtTextToSpeech import QTextToSpeech
from okx_gui.audio import WaveSpeechEngine, local_speech_commands


def positive_decimal(text):
    try:
        value = Decimal(text.strip())
    except (InvalidOperation, ValueError):
        raise ValueError("价格和价格间隔必须是大于 0 的有效数字。") from None
    if not value.is_finite() or value <= 0:
        raise ValueError("价格和价格间隔必须是大于 0 的有效数字。")
    return value


def spoken_price(price, chinese):
    """Speak every digit literally, including zeros and the decimal point."""
    number = format(positive_decimal(str(price)), "f")
    integer, point, fraction = number.partition(".")
    digits = "零一二三四五六七八九" if chinese else (
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"
    )
    spoken_integer = " ".join(digits[int(digit)] for digit in integer)
    if not point:
        return spoken_integer
    spoken_fraction = " ".join(digits[int(digit)] for digit in fraction)
    point_word = "点" if chinese else "point"
    return f"{spoken_integer} {point_word} {spoken_fraction}"


class PriceLadder:
    """Speak only outside an inclusive quiet range, using boundary-based steps."""

    def __init__(self, lower, upper, step, *, allow_once=False):
        lower_text, upper_text = str(lower).strip(), str(upper).strip()
        try:
            self.lower = Decimal(lower_text) if lower_text else Decimal(0)
        except InvalidOperation:
            raise ValueError("低价格必须为大于或等于 0 的有效数字，或留空。") from None
        if not self.lower.is_finite() or self.lower < 0:
            raise ValueError("低价格必须为大于或等于 0 的有效数字，或留空。")
        self.upper = positive_decimal(upper_text) if upper_text else Decimal("Infinity")
        if self.lower >= self.upper:
            raise ValueError("左侧低价格必须小于右侧高价格。")
        step_text = str(step).strip()
        if not step_text and not allow_once:
            raise ValueError("请填写价格间隔；如需只播报一次价格，请先开启该合约的“情况紧急”。")
        if not step_text and not lower_text and not upper_text:
            raise ValueError("单次价格播报模式请至少填写一个价格边界。")
        self.step = positive_decimal(step_text) if step_text else None
        self.current_based = not lower_text and not upper_text
        self.base = None
        self.once_fired = False
        self.level = None
        self.side = None

    def validate_current(self, price):
        price = positive_decimal(str(price))
        if not self.lower <= price <= self.upper:
            raise ValueError(f"当前价格 {price} 不处于设置范围 [{self.lower}, {self.upper}] 内，请调整价格范围。")
        if self.current_based and self.base is None:
            self.base = self.level = price

    def feed(self, price):
        price = positive_decimal(str(price))
        if self.current_based:
            if self.base is None:
                self.base = self.level = price
                return False
            distance = price - self.level
            steps = (abs(distance) / self.step).to_integral_value(rounding=ROUND_FLOOR)
            if not steps:
                return False
            self.side = "high" if distance > 0 else "low"
            self.level += steps * self.step if distance > 0 else -steps * self.step
            return True
        if self.lower <= price <= self.upper:
            self.level = self.side = None
            return False
        side = "low" if price < self.lower else "high"
        if self.step is None:
            self.side = side
            if self.once_fired:
                return False
            self.once_fired = True
            return True
        boundary = self.lower if side == "low" else self.upper
        if self.side != side:
            self.side = side
            steps = (abs(price - boundary) / self.step).to_integral_value(rounding=ROUND_FLOOR)
            self.level = boundary + (steps * self.step if side == "high" else -steps * self.step)
            return True
        distance = price - self.level
        steps = (abs(distance) / self.step).to_integral_value(rounding=ROUND_FLOOR)
        if not steps:
            return False
        self.level += steps * self.step if distance > 0 else -steps * self.step
        return True


class SpeechService(QObject):
    error = Signal(str)
    EMERGENCY = "__emergency__"

    def __init__(self, parent=None, *, prefer_native=True, max_pending=5):
        super().__init__(parent)
        if max_pending < 1:
            raise ValueError("播报队列容量必须至少为 1")
        self.pending = deque(maxlen=max_pending)
        self.current = None
        self._speaking = False
        self.emergency_enabled = set()
        self.emergency_active = {}
        self.emergency_current = None
        commands = local_speech_commands() if prefer_native else None
        if commands:
            self.tts = WaveSpeechEngine(commands, self)
        else:
            engines = [e for e in QTextToSpeech.availableEngines() if e != "mock"]
            self.tts = QTextToSpeech(engines[0], self) if engines else None
        self.chinese = False
        if self.tts:
            self.tts.setVolume(0.5)
            locales = self.tts.availableLocales()
            chinese = next((l for l in locales if l.language() == QLocale.Chinese), None)
            english = next((l for l in locales if l.name() == "en_US"), None)
            # speechd exposes eSpeak's "cmn" language as QLocale.C, so it is
            # absent from the Chinese locale list. Select its real voice by name.
            mandarin = None
            if self.tts.engine() == "speechd":
                c_locale = next((l for l in locales if l.language() == QLocale.C), None)
                if c_locale is not None:
                    self.tts.setLocale(c_locale)
                    mandarin = next((v for v in self.tts.availableVoices()
                                     if v.name() == "Chinese (Mandarin)"), None)
            if mandarin is not None:
                self.tts.setVoice(mandarin)
            elif chinese or english:
                self.tts.setLocale(chinese or english)
                if self.tts.engine() == "speechd":
                    # Avoid MaryTTS voices (dfki-*), which require a separate server.
                    offline = next((v for v in self.tts.availableVoices()
                                    if v.name() == "English (America)"), None)
                    if offline is not None:
                        self.tts.setVoice(offline)
            self.chinese = mandarin is not None or chinese is not None
            self.tts.stateChanged.connect(self._state_changed)
            self.tts.errorOccurred.connect(lambda *args: self._failed())

    def availability_error(self):
        if not self.tts:
            return "未找到系统语音引擎，请安装 speech-dispatcher 和语音引擎后重启。"
        if self.tts.state() == QTextToSpeech.State.Error:
            return f"语音引擎不可用：{self.tts.errorString()}"
        return ""

    def set_volume(self, percent):
        if self.tts:
            self.tts.setVolume(max(0, min(100, percent)) / 100)

    def set_emergency(self, instrument, enabled, *, side=None):
        instrument = (instrument, side) if side is not None else instrument
        if enabled:
            self.emergency_enabled.add(instrument)
        else:
            self.emergency_enabled.discard(instrument)
            self.emergency_active.pop(instrument, None)
            if self.current == self.EMERGENCY and self.emergency_current == instrument:
                self.cancel(self.EMERGENCY)

    def trigger_emergency(self, instrument, side):
        key = (instrument, side)
        if key in self.emergency_enabled:
            self.emergency_active.setdefault(key, None)
            self._next()

    def announce(self, instrument, price, *, alarm=True, side=None):
        key = (instrument, side) if side is not None else instrument
        if alarm and key in self.emergency_enabled:
            self.emergency_active.setdefault(key, None)
        # A full deque evicts only the oldest waiting utterance, never the active one.
        self.pending.append((instrument, price))
        self._next()

    def _next(self):
        if self.availability_error() or self.current is not None:
            return
        if self.tts.state() != QTextToSpeech.State.Ready:
            return
        if not self.pending:
            if self.emergency_active:
                instrument = next(iter(self.emergency_active))
                self.emergency_active.pop(instrument)
                self.emergency_active[instrument] = None  # Round-robin across active contracts.
                self.emergency_current = instrument
                self.current = self.EMERGENCY
                self._speaking = False
                self.tts.say("情况紧急" if self.chinese else "Emergency")
            return
        instrument, price = self.pending.popleft()
        self.current = instrument
        self._speaking = False
        name = instrument.split("-", 1)[0]
        price = spoken_price(price, self.chinese)
        text = f"{name}，价格 {price}" if self.chinese else f"{name}, price {price}"
        self.tts.say(text)

    def _state_changed(self, state):
        if state == QTextToSpeech.State.Speaking:
            self._speaking = True
        elif (state == QTextToSpeech.State.Ready and self._speaking
              and self.tts.state() == QTextToSpeech.State.Ready):
            self._speaking = False
            self.current = None
            self.emergency_current = None
            QTimer.singleShot(0, self._next)

    def _failed(self):
        self.emergency_active.clear()
        self.emergency_enabled.clear()
        self.emergency_current = None
        self.pending.clear()
        self.current = None
        self._speaking = False
        self.error.emit(self.availability_error() or "语音播放失败，请检查系统音频设置。")

    def shutdown(self):
        self.emergency_enabled.clear()
        self.emergency_active.clear()
        self.cancel()
        if self.tts and hasattr(self.tts, "shutdown"):
            self.tts.shutdown()

    def cancel(self, instrument=None, *, interrupt=True):
        if instrument is None:
            self.pending.clear()
        else:
            retained = [item for item in self.pending if item[0] != instrument]
            self.pending.clear()
            self.pending.extend(retained)
        if interrupt and (instrument is None or self.current == instrument):
            self.current = None
            self.emergency_current = None
            self._speaking = False
            if self.tts:
                self.tts.stop()
            QTimer.singleShot(0, self._next)
