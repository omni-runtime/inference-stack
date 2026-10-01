#!/usr/bin/env python3
"""Probe configured real backends only through SR; retain native upstream evidence."""
import argparse
import json
from pathlib import Path

from run import ROOT, Run


class RealRun(Run):
    def execute(self):
        if self.args.overlay != 'real':
            raise ValueError('real test runner requires mode: real')
        self.wait_gateway()
        chat = json.loads((ROOT / "tests/requests/chat-text.json").read_text())
        speech = json.loads((ROOT / "tests/requests/speech.json").read_text())
        chat_models = [m for m in self.models if m["api_format"] == "openai" and "text" in m["capabilities"]]
        speech_models = [m for m in self.models if m["api_format"] == "speech"]
        chat_backends = [m["service"] for m in chat_models]
        speech_backends = [m["service"] for m in speech_models]
        self.call("real-unauthenticated", "/v1/chat/completions", chat, [401], token=False)
        self.call("real-chat-auto", "/v1/chat/completions", chat,
                  [200] if chat_backends else [503], chat_backends, kind="chat")
        self.call("real-chat-sse", "/v1/chat/completions", {**chat,"stream":True},
                  [200] if chat_backends else [503], chat_backends, kind="sse")
        for recipe, pool_set in [("local-only", {"vllm", "omni"}), ("cloud-only", {"cloud"})]:
            allowed = [m["service"] for m in chat_models if m["pool"] in pool_set]
            registered = bool(set(self.pools) & pool_set)
            self.call("real-"+recipe+"-forged-header", "/v1/chat/completions", {**chat,"model":recipe},
                      [200] if allowed else ([503] if registered else [400]), allowed, kind="chat",
                      headers={"X-Selected-Model":"cloud/chat", "X-VSR-Skip-Processing":"true"})
        self.call("real-explicit-model-rejected", "/v1/chat/completions", {**chat,"model":self.models[0]["name"]}, [400])
        vision_models = [m for m in chat_models if "image_input" in m["capabilities"]]
        vision = json.loads((ROOT / "tests/requests/chat-vision.json").read_text())
        vision["max_tokens"] = 2048
        vision_backends = [m["service"] for m in vision_models]
        text = ""
        prose = self.call("real-vision", "/v1/chat/completions", vision,
                          [200] if vision_backends else [503], vision_backends, kind="chat")
        if vision_backends and self.records[-1]["status"] == 200:
            text = json.loads(prose)["choices"][0]["message"]["content"]
            self.records[-1]["blue_fixture_recognized"] = "blue" in text.lower() or "蓝" in text
            if not self.records[-1]["blue_fixture_recognized"]:
                self.records[-1]["error"] = "vision response did not identify the synthetic blue square"
        self.call("real-speech", "/v1/audio/speech", speech,
                  [200] if speech_backends else [503], speech_backends, kind="wav")
        if speech_backends:
            self.call("real-speech-sse", "/v1/audio/speech", {**speech,"stream_format":"sse","response_format":"pcm"}, [200], speech_backends, kind="speech-sse")
            if vision_backends and text:
                # An explicit second client request. Keep the short fixture
                # within this GPU's declared audio output budget.
                self.call("real-caller-vision-then-speech", "/v1/audio/speech", {**speech,"input":text[:120]}, [200], speech_backends, kind="wav")
        return self.collect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--environment")
    parser.add_argument("--runtime", choices=["kubernetes", "docker"])
    parser.add_argument("--example")
    parser.add_argument("--catalog")
    parser.add_argument("--overlay", choices=["real"])
    parser.add_argument("--url")
    args = parser.parse_args()
    if not args.config:
        args.overlay = args.overlay or 'real'
    raise SystemExit(1 if RealRun(args).execute() else 0)
