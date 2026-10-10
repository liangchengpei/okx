import pytest
from okx_gui.synthesize import split_contract_name


@pytest.mark.parametrize("name,letters", [
    ("BTC", "B T C"), ("ETH", "E T H"),
    ("SPCX", "S P C X"), ("1INCH", "1 I N C H"),
])
def test_english_name_keeps_chinese_price(name, letters):
    price = "价格 八 二 六 零 零 点 零 五"
    assert split_contract_name(f"{name}，{price}") == (letters, price)


def test_emergency_and_english_messages_stay_unchanged():
    for text in ("情况紧急", "BTC, price eight two six zero zero", "预热"):
        assert split_contract_name(text) == (None, text)


def test_english_audio_uses_english_voice_and_reuses_cache(monkeypatch):
    import io
    import wave
    from types import SimpleNamespace
    from okx_gui import synthesize

    buffer = io.BytesIO()
    frames = b"\x01\x00" * 100
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(frames)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=buffer.getvalue())

    monkeypatch.setattr(synthesize.shutil, "which", lambda name: "/test/espeak-ng")
    monkeypatch.setattr(synthesize.subprocess, "run", run)
    synthesize.english_name_audio.cache_clear()
    try:
        assert synthesize.english_name_audio("B T C", 22050) == frames
        assert synthesize.english_name_audio("B T C", 22050) == frames
        assert calls == [["/test/espeak-ng", "-v", "en-us", "-s", "160", "--stdout", "B T C"]]
    finally:
        synthesize.english_name_audio.cache_clear()


def test_neural_english_name_uses_cached_model_audio():
    from types import SimpleNamespace
    from okx_gui.synthesize import NeuralEnglishNames

    calls = []

    class FakeVoice:
        config = SimpleNamespace(sample_rate=22050)

        def synthesize(self, text, syn_config=None):
            calls.append(text)
            yield SimpleNamespace(audio_int16_bytes=b"\x01\x00" * 10)
            yield SimpleNamespace(audio_int16_bytes=b"\x02\x00" * 10)

    names = NeuralEnglishNames(FakeVoice())
    expected = b"\x01\x00" * 10 + b"\x02\x00" * 10
    assert names.audio("B T C", 22050) == expected
    assert names.audio("B T C", 22050) == expected
    names.audio("S P C X", 22050)
    assert calls == ["B T C", "S P C X"]
