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
    synth = executable(tmp_path / "neural", f"""import sys, json
from pathlib import Path
print(json.dumps({{"ready":True}}),flush=True)
for line in sys.stdin:
 request=json.loads(line)
 Path({str(received)!r}).write_text(request["text"])
 Path(request["output"]).write_bytes(b"RIFF"+b"0"*100)
 print(json.dumps({{"id":request["id"],"ok":True}}),flush=True)
""")
    player = executable(tmp_path / "play", f'from pathlib import Path\nPath({str(marker)!r}).touch()\n')
    engine = WaveSpeechEngine((synth, player, str(tmp_path / "voice.onnx")))
    assert engine.engine() == "piper-wave"
    engine.say("BTC 价格 82600 点 零五")
    qtbot.waitUntil(lambda: engine.state() == QTextToSpeech.State.Ready)
    assert received.read_text() == "BTC 价格 82600 点 零五"
    assert marker.exists()
    pid = engine.synth_process.processId()
    engine.say("第二次播报")
    qtbot.waitUntil(lambda: engine.state() == QTextToSpeech.State.Ready)
    assert engine.synth_process.processId() == pid
    assert received.read_text() == "第二次播报"
    engine.shutdown()
    assert engine.synth_process.state() == QProcess.NotRunning


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


def test_cancelled_neural_result_is_ignored_and_model_stays_loaded(qtbot, tmp_path):
    received = tmp_path / "started"
    played = tmp_path / "played"
    synth = executable(tmp_path / "neural", f'''import sys,json,time
from pathlib import Path
print(json.dumps({{"ready":True}}),flush=True)
for line in sys.stdin:
 request=json.loads(line)
 Path({str(received)!r}).touch()
 time.sleep(0.15)
 try:
  Path(request["output"]).write_bytes(b"RIFF"+b"0"*100)
  response={{"id":request["id"],"ok":True}}
 except Exception:
  response={{"id":request["id"],"error":"cancelled file removed"}}
 print(json.dumps(response),flush=True)
''')
    player = executable(tmp_path / "play", f'''from pathlib import Path
path=Path({str(played)!r})
path.write_text(path.read_text()+"1" if path.exists() else "1")
''')
    engine = WaveSpeechEngine((synth, player, "voice.onnx"))
    try:
        engine.say("cancel this")
        qtbot.waitUntil(received.exists)
        pid = engine.synth_process.processId()
        engine.stop()
        engine.say("play this")
        qtbot.waitUntil(lambda: engine.state() == QTextToSpeech.State.Ready)
        assert engine.synth_process.processId() == pid
        assert played.read_text() == "1"
    finally:
        engine.shutdown()
