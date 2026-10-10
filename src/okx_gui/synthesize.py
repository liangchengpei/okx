"""Offline neural synthesis with a persistent, preloaded model process."""
import argparse
import json
import sys
import wave


def main():
    from piper import PiperVoice, SynthesisConfig

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--output")
    parser.add_argument("--server", action="store_true")
    args = parser.parse_args()
    voice = PiperVoice.load(args.model)

    def synthesize(text, output):
        with wave.open(output, "wb") as wav:
            voice.synthesize_wav(text, wav, syn_config=SynthesisConfig(length_scale=1.0))

    if not args.server:
        if not args.output:
            parser.error("--output is required without --server")
        synthesize(sys.stdin.buffer.read().decode("utf-8"), args.output)
        return

    # Warm inference kernels without playing audio before accepting requests.
    for _ in voice.synthesize("预热"):
        pass
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
