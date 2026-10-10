"""Isolated offline neural speech synthesis, cancellable by the GUI."""
import argparse
import sys
import wave


def main():
    from piper import PiperVoice, SynthesisConfig

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    text = sys.stdin.buffer.read().decode("utf-8")
    voice = PiperVoice.load(args.model)
    with wave.open(args.output, "wb") as wav:
        voice.synthesize_wav(text, wav, syn_config=SynthesisConfig(length_scale=1.0))


if __name__ == "__main__":
    main()
