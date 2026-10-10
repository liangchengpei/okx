"""Offline neural synthesis with a persistent, preloaded model process."""
import argparse
import json
import re
import io
import shutil
import subprocess
from pathlib import Path
from functools import lru_cache
import sys
import wave


def split_contract_name(text):
    """Keep the Chinese price intact and spell the contract in English."""
    match = re.match(r"([A-Za-z0-9]+)，(?=价格 )", text)
    if not match:
        return None, text
    return " ".join(match[1].upper()), text[match.end():]


@lru_cache(maxsize=256)
def english_name_audio(letters, sample_rate):
    """Cache short English names; never ask the Mandarin model to pronounce them."""
    executable = shutil.which("espeak-ng")
    if not executable:
        local = Path(__file__).resolve().parents[2] / ".local/espeak/usr/bin/espeak-ng"
        if local.is_file():
            executable = str(local)
    if not executable:
        raise RuntimeError("合约英文发音需要 espeak-ng，请安装后重启。")
    result = subprocess.run(
        [executable, "-v", "en-us", "-s", "160", "--stdout", letters],
        capture_output=True, check=True, timeout=5,
    )
    with wave.open(io.BytesIO(result.stdout), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("英文语音音频格式不受支持")
        frames = wav.readframes(wav.getnframes())
        source_rate = wav.getframerate()
    if source_rate != sample_rate:
        import numpy as np

        samples = np.frombuffer(frames, dtype="<i2")
        count = round(len(samples) * sample_rate / source_rate)
        frames = np.interp(
            np.arange(count) * source_rate / sample_rate,
            np.arange(len(samples)), samples,
        ).astype("<i2").tobytes()
    return frames


class NeuralEnglishNames:
    """Reuse one English voice and cache complete names in the resident worker."""

    def __init__(self, voice, syn_config=None):
        self.voice = voice
        self.syn_config = syn_config

    @lru_cache(maxsize=256)
    def audio(self, letters, sample_rate):
        frames = b"".join(chunk.audio_int16_bytes for chunk in self.voice.synthesize(letters, syn_config=self.syn_config))
        source_rate = self.voice.config.sample_rate
        if source_rate != sample_rate:
            import numpy as np

            samples = np.frombuffer(frames, dtype="<i2")
            count = round(len(samples) * sample_rate / source_rate)
            frames = np.interp(
                np.arange(count) * source_rate / sample_rate,
                np.arange(len(samples)), samples,
            ).astype("<i2").tobytes()
        return frames


def main():
    from piper import PiperVoice, SynthesisConfig

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--output")
    parser.add_argument("--english-model", help="English Piper model; defaults to en_US-amy-medium beside --model")
    parser.add_argument("--server", action="store_true")
    args = parser.parse_args()
    voice = PiperVoice.load(args.model)
    english_audio = english_name_audio
    english_voice = None
    english_path = Path(args.english_model) if args.english_model else (
        Path(args.model).with_name("en_US-amy-medium.onnx")
    )
    if voice.config.espeak_voice == "cmn" and english_path.is_file():
        english_voice = PiperVoice.load(str(english_path))
        english_audio = NeuralEnglishNames(
            english_voice, SynthesisConfig(length_scale=0.9)
        ).audio

    def synthesize(text, output):
        letters, chinese_text = split_contract_name(text)
        if voice.config.espeak_voice != "cmn":
            letters, chinese_text = None, text
        # Write one complete WAV so playback, volume and cancellation remain atomic.
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(voice.config.sample_rate)
            if letters:
                wav.writeframes(english_audio(letters, voice.config.sample_rate))
                wav.writeframes(b"\x00\x00" * round(voice.config.sample_rate * 0.08))
            for chunk in voice.synthesize(chinese_text, syn_config=SynthesisConfig(length_scale=1.0)):
                wav.writeframes(chunk.audio_int16_bytes)

    if not args.server:
        if not args.output:
            parser.error("--output is required without --server")
        synthesize(sys.stdin.buffer.read().decode("utf-8"), args.output)
        return

    # Warm inference kernels without playing audio before accepting requests.
    for _ in voice.synthesize("预热"):
        pass
    if english_voice is not None:
        for letters in ("B T C", "S P C X"):
            english_audio(letters, voice.config.sample_rate)
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        request = json.loads(line)
        try:
            synthesize(request["text"], request["output"])
            response = {"id": request["id"], "ok": True}
        except Exception as exc:
            response = {"id": request["id"], "error": str(exc)}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
