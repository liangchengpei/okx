import sys
from pathlib import Path

from PySide6.QtCore import QProcess
from PySide6.QtTextToSpeech import QTextToSpeech

from okx_gui.audio import WaveSpeechEngine


def executable(path, body):
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o755)
    return str(path)


def test_audio_file_generated_then_played_and_deleted(qtbot, tmp_path):
    marker = tmp_path / "played"
    synth = executable(tmp_path / "synth", "import sys\nfrom pathlib import Path\nPath(sys.argv[sys.argv.index('-w')+1]).write_bytes(b'RIFF'+b'0'*100)\n")
    play = executable(tmp_path / "play", f"""import sys
from pathlib import Path
assert Path(sys.argv[-1]).exists()
Path({str(marker)!r}).write_text(sys.argv[-1] + chr(10) + " ".join(sys.argv[1:-1]))
""")
    engine = WaveSpeechEngine((synth, play))
    engine.setVolume(0.35)
    engine.say("BTC 价格 82600")
    qtbot.waitUntil(lambda: engine.state() == QTextToSpeech.State.Ready)
    assert marker.exists()
    recorded = marker.read_text().splitlines()
    assert not Path(recorded[0]).exists()
    assert "--volume=22938" in recorded[1]
    assert engine.process.state() == QProcess.NotRunning


def test_audio_process_failure_is_reported(qtbot, tmp_path):
    engine = WaveSpeechEngine((str(tmp_path / "missing-synth"), "missing-player"))
    with qtbot.waitSignal(engine.errorOccurred):
        engine.say("test")
    assert engine.state() == QTextToSpeech.State.Error
    assert "无法启动" in engine.errorString()
    assert engine._directory is None


def test_audio_cancel_kills_process_and_cleans_file(qtbot, tmp_path):
    synth = executable(tmp_path / "slow-synth", "import time\ntime.sleep(30)\n")
    engine = WaveSpeechEngine((synth, "unused-player"))
    engine.say("test")
    directory = engine._directory.name
    qtbot.waitUntil(lambda: engine.process.state() == QProcess.Running)
    engine.say("must not interrupt active audio")
    assert engine._directory.name == directory
    assert engine.process.state() == QProcess.Running
    engine.stop()
    assert engine.state() == QTextToSpeech.State.Ready
    assert engine.process.state() == QProcess.NotRunning
    assert not Path(directory).exists()


def test_neural_synthesis_receives_utf8_text_then_plays(qtbot, tmp_path):
    received = tmp_path / "received"
    marker = tmp_path / "played"
    synth = executable(tmp_path / "neural", f'''import sys
from pathlib import Path
text = sys.stdin.buffer.read().decode("utf-8")
Path({str(received)!r}).write_text(text)
Path(sys.argv[sys.argv.index("--output")+1]).write_bytes(b"RIFF"+b"0"*100)
''')
    player = executable(tmp_path / "play", f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    engine = WaveSpeechEngine((synth, player, str(tmp_path / "voice.onnx")))
    assert engine.engine() == "piper-wave"
    engine.say("BTC 价格 82600 点 零五")
    qtbot.waitUntil(lambda: engine.state() == QTextToSpeech.State.Ready)
    assert received.read_text() == "BTC 价格 82600 点 零五"
    assert marker.exists()


def test_repeated_emergency_reuses_audio_without_resynthesis(qtbot, tmp_path):
    counter = tmp_path / "count"
    synth = executable(tmp_path / "synth", f'''import sys
from pathlib import Path
counter=Path({str(counter)!r})
counter.write_text(counter.read_text()+"1" if counter.exists() else "1")
Path(sys.argv[sys.argv.index("-w")+1]).write_bytes(b"RIFF"+b"0"*100)
''')
    play = executable(tmp_path / "play", "pass\n")
    engine = WaveSpeechEngine((synth, play))
    for _ in range(2):
        engine.say("情况紧急")
        qtbot.waitUntil(lambda: engine.state() == QTextToSpeech.State.Ready)
    assert counter.read_text() == "1"
