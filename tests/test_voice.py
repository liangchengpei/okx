from decimal import Decimal

import pytest

from okx_gui.voice import PriceLadder, positive_decimal


def test_quiet_range_includes_boundaries_and_reentry():
    ladder = PriceLadder("82400", "82800", "100")
    for price in ("82600", "82400", "82800"):
        assert not ladder.feed(price)
    assert ladder.feed("82801")
    assert not ladder.feed("82899")
    assert ladder.feed("82900")
    assert not ladder.feed("82900")
    assert not ladder.feed("82850")
    assert not ladder.feed("82800")
    assert ladder.side is None
    assert ladder.feed("82801")
    assert not ladder.feed("82600")
    assert ladder.feed("82399")
    assert ladder.feed("82300")
    assert not ladder.feed("82350")
    assert not ladder.feed("82400")


def test_jump_between_sides_and_small_decimal_steps():
    ladder = PriceLadder("0.10", "0.20", "0.01")
    assert not ladder.feed("0.15")
    assert ladder.feed("0.235")
    assert ladder.level == Decimal("0.23")
    assert not ladder.feed("0.235")
    assert ladder.feed("0.085")
    assert ladder.level == Decimal("0.09")
    assert not ladder.feed("0.085")
    assert not ladder.feed("0.10")


def test_current_price_must_be_inside_range():
    ladder = PriceLadder("82400", "82800", "100")
    for price in ("82400", "82600", "82800"):
        ladder.validate_current(price)
    for price in ("82399", "82801"):
        with pytest.raises(ValueError, match="不处于设置范围"):
            ladder.validate_current(price)


@pytest.mark.parametrize("value", ["", "NaN", "Infinity", "-1", "0", "words"])
def test_invalid_parameters(value):
    with pytest.raises(ValueError):
        positive_decimal(value)


def test_speech_queue_waits_evicts_oldest_and_cancels(monkeypatch, qtbot):
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtTextToSpeech import QTextToSpeech
    from okx_gui import voice

    class FakeTTS(QObject):
        State = QTextToSpeech.State
        stateChanged = Signal(object)
        errorOccurred = Signal()

        @staticmethod
        def availableEngines():
            return ["test"]

        def __init__(self, engine, parent):
            super().__init__(parent)
            self.calls = []
            self.current_state = self.State.Ready

        def availableLocales(self):
            return []

        def setVolume(self, volume):
            self.volume = volume

        def engine(self):
            return "test"

        def state(self):
            return self.current_state

        def errorString(self):
            return "device failed"

        def say(self, text):
            self.calls.append(text)
            self.current_state = self.State.Speaking
            self.stateChanged.emit(self.current_state)

        def stop(self):
            self.current_state = self.State.Ready
            self.stateChanged.emit(self.current_state)

    monkeypatch.setattr(voice, "QTextToSpeech", FakeTTS)
    service = voice.SpeechService(prefer_native=False, max_pending=2)
    service.announce("BTC-USDT-SWAP", "82600")
    service.announce("ETH-USDT-SWAP", "3000")
    service.announce("ETH-USDT-SWAP", "3010")
    service.announce("SOL-USDT-SWAP", "100")
    assert list(service.pending) == [("ETH-USDT-SWAP", "3010"), ("SOL-USDT-SWAP", "100")]
    assert service.current == "BTC-USDT-SWAP"
    service.tts.stateChanged.emit(service.tts.State.Ready)  # Ignore a stale notification.
    assert service.current == "BTC-USDT-SWAP"
    service.cancel("BTC-USDT-SWAP", interrupt=False)
    assert service.current == "BTC-USDT-SWAP"
    service.cancel("SOL-USDT-SWAP")
    assert len(service.tts.calls) == 1
    service.tts.stop()
    qtbot.waitUntil(lambda: len(service.tts.calls) == 2)
    assert "3010" in service.tts.calls[-1]
    assert "3000" not in service.tts.calls[-1]
    service.announce("ETH-USDT-SWAP", "3020")
    service.cancel("ETH-USDT-SWAP", interrupt=False)
    assert service.current == "ETH-USDT-SWAP"
    assert not service.pending
    service.cancel()
    assert not service.pending
    assert service.current is None


def test_missing_speech_engine_is_reported(monkeypatch):
    from okx_gui import voice
    monkeypatch.setattr(voice.QTextToSpeech, "availableEngines", lambda: ["mock"])
    service = voice.SpeechService(prefer_native=False)
    assert service.availability_error()
    assert service.tts is None


def test_speechd_selects_real_mandarin_voice_and_sets_volume(monkeypatch):
    from PySide6.QtCore import QObject, Signal, QLocale
    from PySide6.QtTextToSpeech import QTextToSpeech
    from okx_gui import voice

    class Voice:
        def __init__(self, name):
            self._name = name

        def name(self):
            return self._name

    class FakeSpeechd(QObject):
        State = QTextToSpeech.State
        stateChanged = Signal(object)
        errorOccurred = Signal()

        @staticmethod
        def availableEngines():
            return ["speechd"]

        def __init__(self, engine, parent):
            super().__init__(parent)
            self.selected = Voice("dfki-spike")
            self.locale = QLocale("en_US")

        def engine(self):
            return "speechd"

        def setVolume(self, volume):
            self.volume = volume

        def availableLocales(self):
            return [QLocale("en_US"), QLocale("C")]

        def setLocale(self, locale):
            self.locale = locale

        def availableVoices(self):
            return [Voice("Chinese (Mandarin)")] if self.locale.language() == QLocale.C else [Voice("dfki-spike"), Voice("English (America)")]

        def setVoice(self, selected):
            self.selected = selected

    monkeypatch.setattr(voice, "QTextToSpeech", FakeSpeechd)
    service = voice.SpeechService(prefer_native=False)
    assert service.tts.selected.name() == "Chinese (Mandarin)"
    assert service.tts.volume == 0.5
    assert service.chinese


@pytest.mark.parametrize("low,high,step", [("-1", "82800", "100"), ("82400", "Infinity", "100"), ("82800", "82400", "100"), ("82600", "82600", "100"), ("NaN", "82800", "100"), ("82400", "82800", "0")])
def test_invalid_range(low, high, step):
    with pytest.raises(ValueError):
        PriceLadder(low, high, step)


@pytest.mark.parametrize("price,chinese,expected", [
    ("82600.05", True, "82600 点 零五"),
    ("0.0012300", True, "0 点 零零一二三零零"),
    ("82600", True, "82600"),
    ("82600.0", True, "82600 点 零"),
    ("1.2E-5", True, "0 点 零零零零一二"),
    ("82600.05", False, "82600 point zero five"),
])
def test_decimal_price_is_spoken_with_point_and_each_digit(price, chinese, expected):
    from okx_gui.voice import spoken_price
    assert spoken_price(price, chinese) == expected


def test_emergency_loops_after_alarm_prioritizes_prices_and_stops(monkeypatch, qtbot):
    from PySide6.QtCore import QObject, Signal, QLocale
    from PySide6.QtTextToSpeech import QTextToSpeech
    from okx_gui import voice

    class FakeNative(QObject):
        stateChanged = Signal(object)
        errorOccurred = Signal()

        def __init__(self, commands, parent):
            super().__init__(parent)
            self.calls = []
            self._state = QTextToSpeech.State.Ready

        def engine(self):
            return "test"

        def availableLocales(self):
            return [QLocale("zh_CN")]

        def setLocale(self, locale):
            pass

        def setVolume(self, volume):
            pass

        def state(self):
            return self._state

        def say(self, text):
            assert self._state == QTextToSpeech.State.Ready
            self.calls.append(text)
            self._state = QTextToSpeech.State.Speaking
            self.stateChanged.emit(self._state)

        def stop(self):
            self._state = QTextToSpeech.State.Ready
            self.stateChanged.emit(self._state)

    monkeypatch.setattr(voice, "local_speech_commands", lambda: ("synth", "player"))
    monkeypatch.setattr(voice, "WaveSpeechEngine", FakeNative)
    service = voice.SpeechService()
    service.set_emergency("BTC", True)
    service._next()
    assert not service.tts.calls  # Enabling alone must stay quiet.
    service.announce("BTC", "82600", alarm=False)
    service.tts.stop()
    qtbot.wait(10)
    assert not service.emergency_active  # Test speech cannot latch the alarm.
    service.announce("BTC", "82700")
    assert service.emergency_active
    service.tts.stop()
    qtbot.waitUntil(lambda: service.current == service.EMERGENCY)
    assert service.tts.calls[-1] == "情况紧急"
    count = len(service.tts.calls)
    service.tts.stop()
    qtbot.waitUntil(lambda: len(service.tts.calls) == count + 1)
    assert service.tts.calls[-1] == "情况紧急"
    service.set_emergency("ETH", True)
    service.announce("ETH", "3000")
    assert service.current == service.EMERGENCY  # Finish current audio first.
    service.tts.stop()
    qtbot.waitUntil(lambda: service.current == "ETH")
    assert "3000" in service.tts.calls[-1]
    service.cancel("ETH", interrupt=False)
    service.tts.stop()
    qtbot.waitUntil(lambda: service.current == service.EMERGENCY)
    service.announce("SOL", "100")
    service.set_emergency("BTC", False)
    qtbot.waitUntil(lambda: service.current == "SOL")
    assert "BTC" not in service.emergency_active
    assert "ETH" in service.emergency_active
    service.tts.stop()
    qtbot.waitUntil(lambda: service.current == service.EMERGENCY)
    assert service.emergency_current == "ETH"
    service.set_emergency("ETH", False)
    qtbot.wait(10)
    assert service.current is None
    service.set_emergency("BTC", True)
    service._next()
    assert service.current is None  # Re-enabling waits for a fresh price alarm.
    service.cancel()
    service.set_emergency("BTC", False)
    service.set_emergency("BTC", True, side="low")
    service.announce("BTC", "83000", side="high")
    assert not service.emergency_active
    service.tts.stop()
    qtbot.waitUntil(lambda: service.current is None)
    service.announce("BTC", "82000", side="low")
    assert ("BTC", "low") in service.emergency_active
    service.set_emergency("BTC", True, side="high")
    service.announce("BTC", "84000", side="high")
    assert ("BTC", "high") in service.emergency_active
    service.set_emergency("BTC", False, side="low")
    assert ("BTC", "low") not in service.emergency_active
    assert ("BTC", "high") in service.emergency_active
    service.set_emergency("BTC", False, side="high")
    service.cancel()


def test_upper_only_defaults_lower_to_zero():
    ladder = PriceLadder("  ", "82800", "100")
    assert ladder.lower == 0
    ladder.validate_current("82600")
    assert not ladder.feed("0.000001")
    assert not ladder.feed("82800")
    assert ladder.feed("82801")
    assert ladder.feed("82900")
    with pytest.raises(ValueError, match="不处于设置范围"):
        ladder.validate_current("82801")


def test_lower_only_defaults_upper_to_infinity():
    ladder = PriceLadder("82400", "", "100")
    assert ladder.upper == Decimal("Infinity")
    ladder.validate_current("1000000000000000")
    assert not ladder.feed("1000000000000000")
    assert not ladder.feed("82400")
    assert ladder.feed("82399")
    assert ladder.feed("82300")
    with pytest.raises(ValueError, match="不处于设置范围"):
        ladder.validate_current("82399")


def test_both_blank_are_unbounded_and_explicit_zero_is_valid():
    for low in ("", "0", "0.0"):
        ladder = PriceLadder(low, "", "100")
        for price in ("0.00001", "1", "1000000000000000000"):
            ladder.validate_current(price)
            assert not ladder.feed(price)


@pytest.mark.parametrize("low,high,first", [("", "82800", "82801"), ("82400", "", "82399")])
def test_empty_step_announces_price_once_until_restarted(low, high, first):
    ladder = PriceLadder(low, high, " ", allow_once=True)
    assert not ladder.feed("82600")
    assert ladder.feed(first)
    for price in ("83000", "82300", "82600", first, "84000"):
        assert not ladder.feed(price)
    assert PriceLadder(low, high, "", allow_once=True).feed(first)


def test_empty_step_requires_emergency_and_at_least_one_boundary():
    with pytest.raises(ValueError, match="情况紧急"):
        PriceLadder("", "82800", "")
    with pytest.raises(ValueError, match="至少填写"):
        PriceLadder("", "", "", allow_once=True)
    with pytest.raises(ValueError):
        PriceLadder("", "82800", "0", allow_once=True)
