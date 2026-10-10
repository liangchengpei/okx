"""Offline neural or eSpeak speech rendered to WAV and played through desktop audio."""
from pathlib import Path
import importlib.util
import shutil
import sys
import tempfile

from PySide6.QtCore import QObject, QLocale, QProcess, QTimer, Signal
from PySide6.QtTextToSpeech import QTextToSpeech


def local_speech_commands():
    if not sys.platform.startswith("linux"):
        return None
    executable = shutil.which("espeak-ng")
    if not executable:
        local = Path(__file__).resolve().parents[2] / ".local/espeak/usr/bin/espeak-ng"
        if local.is_file():
            executable = str(local)
    player = shutil.which("paplay")
    model = Path(__file__).resolve().parents[2] / ".local/voices/zh_CN-huayan-medium.onnx"
    if (player and model.is_file() and Path(str(model) + ".json").is_file()
            and importlib.util.find_spec("piper") is not None):
        return (sys.executable, player, str(model))
    return (executable, player) if executable and player else None


class WaveSpeechEngine(QObject):
    stateChanged = Signal(object)
    errorOccurred = Signal()

    def __init__(self, commands, parent=None):
        super().__init__(parent)
        self.commands = commands
        self.model = commands[2] if len(commands) > 2 else None
        self.process = QProcess(self)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.timeout = QTimer(self)
        self.timeout.setSingleShot(True)
        self.timeout.setInterval(30000)
        self.timeout.timeout.connect(lambda: self._fail("语音合成或播放超时"))
        self._state = QTextToSpeech.State.Ready
        self._error = ""
        self._phase = None
        self._directory = None
        self._locale = QLocale("zh_CN")
        self._volume = 1.0
        self._emergency_cache = None

    def engine(self):
        return "piper-wave" if self.model else "espeak-wave"

    def availableLocales(self):
        return [QLocale("zh_CN"), QLocale("en_US")]

    def setLocale(self, locale):
        self._locale = locale

    def setVolume(self, volume):
        self._volume = max(0.0, min(1.0, volume))

    def state(self):
        return self._state

    def errorString(self):
        return self._error

    def _set_state(self, state):
        self._state = state
        self.stateChanged.emit(state)

    def say(self, text):
        if self._state != QTextToSpeech.State.Ready:
            return  # The queue owns scheduling; never preempt an active utterance.
        self._directory = tempfile.TemporaryDirectory(prefix="okx-speech-")
        self._wave = str(Path(self._directory.name) / "speech.wav")
        self._phase = "synthesize"
        self._text = text
        self._error = ""
        self._set_state(QTextToSpeech.State.Speaking)
        self.timeout.start()
        if text == "情况紧急" and self._emergency_cache is not None:
            shutil.copyfile(Path(self._emergency_cache.name) / "emergency.wav", self._wave)
            self._finished(0, QProcess.NormalExit)
            return
        language = "cmn" if self._locale.language() == QLocale.Chinese else "en-us"
        if self.model:
            self.process.start(self.commands[0], ["-m", "okx_gui.synthesize", "--model", self.model,
                                                 "--output", self._wave])
            self.process.write(text.encode("utf-8"))
            self.process.closeWriteChannel()
        else:
            self.process.start(self.commands[0], ["-v", language, "-s", "150", "-a", "100", "-w", self._wave, text])

    def _finished(self, code, exit_status):
        if self._phase is None:
            return
        if code != 0 or exit_status != QProcess.NormalExit:
            details = bytes(self.process.readAllStandardError()).decode(errors="replace").strip()
            self._fail(f"语音{ '合成' if self._phase == 'synthesize' else '播放' }失败：{details or code}")
            return
        if self._phase == "synthesize":
            path = Path(self._wave)
            if not path.exists() or path.stat().st_size <= 44:
                self._fail("语音合成未产生有效音频")
                return
            if self._text == "情况紧急" and self._emergency_cache is None:
                self._emergency_cache = tempfile.TemporaryDirectory(prefix="okx-emergency-")
                shutil.copyfile(path, Path(self._emergency_cache.name) / "emergency.wav")
            self._phase = "play"
            self.process.start(self.commands[1], ["--client-name=OKX Trader", "--stream-name=合约语音播报",
                                                f"--volume={round(self._volume * 65536)}", self._wave])
        else:
            self._cleanup()
            self._set_state(QTextToSpeech.State.Ready)

    def _process_error(self, error):
        if self._phase is not None:
            self._fail(f"无法启动语音进程：{self.process.errorString()}")

    def _cleanup(self):
        self.timeout.stop()
        self._phase = None
        if self._directory:
            self._directory.cleanup()
            self._directory = None

    def _fail(self, message):
        self.stop()
        self._error = message
        self._set_state(QTextToSpeech.State.Error)
        self.errorOccurred.emit()

    def stop(self):
        self._phase = None
        if self.process.state() != QProcess.NotRunning:
            self.process.kill()
            self.process.waitForFinished(300)
        self._cleanup()
        if self._state != QTextToSpeech.State.Ready:
            self._set_state(QTextToSpeech.State.Ready)
