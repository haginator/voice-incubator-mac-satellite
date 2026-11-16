"""Virtual Voice Incubator satellite for macOS.

Speaks satellite protocol 2 (firmware-voice-incubator/PROTOCOL.md) as
model "linux": microphone, speaker, its own wake word (microWakeWord, local),
LED ring and live debug in a small web GUI.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import random
import socket
import struct
import threading
import time
import webbrowser
from pathlib import Path

import numpy as np
import sounddevice as sd
from aiohttp import web, WSMsgType
from pymicro_wakeword import MicroWakeWord, MicroWakeWordFeatures, Model
from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

LOG = logging.getLogger("satellite")
HERE = Path(__file__).parent
CONFIG = HERE / "config.json"

RATE = 16000
CHUNK = 320  # 10 ms of samples
HEADER = struct.Struct(">BH")
MAX_PAYLOAD = 4096
PREROLL_MS = 1000
CALL_TIMEOUT = 30.0

# device -> app
HELLO, CALL_START, AUDIO_IN, CALL_END, STATUS, PLAYBACK_DRAINED = 0x01, 0x02, 0x03, 0x04, 0x05, 0x06
DEVICE_EVENT, AUDIO_MONITOR, WAKE_REPORT = 0x09, 0x0A, 0x0B
# app -> device
AUDIO_OUT, AUDIO_OUT_END, EVENT, HANGUP, MONITOR = 0x10, 0x11, 0x12, 0x13, 0x14
MODEL_START, MODEL_DATA, MODEL_END, MODEL_CLEAR = 0x20, 0x21, 0x22, 0x23
NAMES = {v: k for k, v in dict(
    HELLO=HELLO, CALL_START=CALL_START, AUDIO_IN=AUDIO_IN, CALL_END=CALL_END, STATUS=STATUS,
    PLAYBACK_DRAINED=PLAYBACK_DRAINED, DEVICE_EVENT=DEVICE_EVENT, AUDIO_MONITOR=AUDIO_MONITOR,
    WAKE_REPORT=WAKE_REPORT, AUDIO_OUT=AUDIO_OUT, AUDIO_OUT_END=AUDIO_OUT_END, EVENT=EVENT,
    HANGUP=HANGUP, MONITOR=MONITOR, MODEL_START=MODEL_START, MODEL_DATA=MODEL_DATA,
    MODEL_END=MODEL_END, MODEL_CLEAR=MODEL_CLEAR).items()}
QUIET = {AUDIO_IN, AUDIO_OUT, AUDIO_MONITOR, STATUS, WAKE_REPORT, MODEL_DATA}

MESSAGES = {
    "no_app": "Wake word / talk without a connected app - ignored",
    "second_conn": "Second connection from {peer} refused",
    "app_connected": "App connected: {peer}",
    "app_disconnected": "App disconnected ({reason})",
    "mic_open": "Microphone open ({name})",
    "call_timeout": "Call timeout (30 s without HANGUP)",
    "wake_monitor": "Wake word detected while monitored (p={p}) - not answered",
    "wake": "Wake word '{name}' p={p}",
    "muted": "Microphone muted",
    "unmuted": "Microphone active",
    "button_cancel": "Button: cancel call",
    "wake_word_set": "Wake word: {name}",
    "vpn_routes": "VPN routes via utun for LAN hosts {hosts} - if the app runs on one of them, "
                  "its connection fails (disconnect the VPN client)",
    "listening": "Satellite listening on {addr} as '{name}' ({mac})",
    "mdns": "mDNS: _voice-incubator._tcp announced",
}

WAKE_NAMES = {"okay_nabu": "Okay Nabu", "hey_jarvis": "Hey Jarvis", "hey_mycroft": "Hey Mycroft", "alexa": "Alexa"}


def load_config() -> dict:
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    if "mac" not in cfg:  # stable, locally administered fake MAC
        b = [0x02] + [random.randint(0, 255) for _ in range(5)]
        cfg["mac"] = ":".join(f"{x:02x}" for x in b)
    cfg.setdefault("name", "mac-satellite")
    cfg.setdefault("wake_word", "okay_nabu")
    cfg.setdefault("muted", False)
    CONFIG.write_text(json.dumps(cfg, indent=2))
    return cfg


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    finally:
        s.close()


class Player:
    """Speaker: a buffer drained by the audio thread."""

    def __init__(self, device):
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.stream = sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16",
                                         blocksize=CHUNK, device=device, callback=self._cb)
        self.stream.start()

    def _cb(self, out, frames, t, status):
        n = len(out)
        with self.lock:
            chunk = bytes(self.buf[:n])
            del self.buf[:n]
        out[:len(chunk)] = chunk
        if len(chunk) < n:
            out[len(chunk):] = b"\x00" * (n - len(chunk))

    def write(self, data: bytes):
        with self.lock:
            self.buf += data

    def clear(self):
        with self.lock:
            self.buf.clear()

    @property
    def queued(self) -> int:
        with self.lock:
            return len(self.buf)


class Satellite:
    def __init__(self, args):
        self.args = args
        self.cfg = load_config()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.mic_q: asyncio.Queue[bytes] = asyncio.Queue()
        self.player = Player(args.output)
        self.writer: asyncio.StreamWriter | None = None
        self.app_peer = None
        self.in_call = False
        self.call_started = 0.0
        self.call_wake = ""
        self.dropping_out = False  # after cancel: drop AUDIO_OUT until AUDIO_OUT_END
        self.out_ended = False  # AUDIO_OUT_END received, waiting to drain
        self.monitor = False
        self.preroll = collections.deque(maxlen=PREROLL_MS // 10)
        self.level = 0.0
        self.prob = 0.0
        self.peak = 0.0
        self.ring: dict = {}  # latest events, for the LED ring
        self.gui: set[web.WebSocketResponse] = set()
        self.logbuf = collections.deque(maxlen=300)
        self.stats = collections.Counter()
        self._load_wake()

    # ---------- wake word ----------
    def _load_wake(self):
        self.wake = MicroWakeWord.from_builtin(Model(self.cfg["wake_word"]))
        self.features = MicroWakeWordFeatures()
        self.cutoff = self.cfg.get("cutoff") or self.wake.probability_cutoff
        self.refractory_until = 0.0

    # ---------- GUI ----------
    def log(self, kind: str, msg: str, data=None, **params):
        """``msg`` is a frame name (rx/tx) or a key of MESSAGES; the GUI
        translates keys itself, the terminal gets English."""
        key = msg if msg in MESSAGES else None
        if key:
            msg = MESSAGES[key].format(**params)
        entry = {"t": time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}",
                 "kind": kind, "msg": msg, "key": key, "params": params, "data": data}
        self.logbuf.append(entry)
        LOG.info("%s %s %s", kind, msg, json.dumps(data, ensure_ascii=False) if data else "")
        self.push({"log": entry})

    def push(self, obj):
        for ws in list(self.gui):
            if not ws.closed:
                asyncio.ensure_future(ws.send_json(obj))

    def state(self):
        return {"state": {
            "connected": self.writer is not None, "peer": self.app_peer,
            "in_call": self.in_call, "muted": self.cfg["muted"], "monitor": self.monitor,
            "wake_word": self.cfg["wake_word"], "wake_words": list(WAKE_NAMES),
            "cutoff": self.cutoff, "name": self.cfg["name"], "mac": self.cfg["mac"],
            "playing": self.player.queued > 0, "ring": self.ring,
        }}

    def push_state(self):
        self.push(self.state())

    def ring_event(self, etype: str, data: dict):
        r = self.ring
        if etype in ("listening", "processing", "recognized", "not_recognized", "responding", "result",
                     "wake_rejected", "wake_overturned"):
            r["phase"] = etype
            r["phase_data"] = data
            r["phase_at"] = time.time()
        elif etype == "timer_progress":
            r["timers"] = data
        elif etype == "timer_ringing":
            r["ringing"] = data
        elif etype == "timer_stopped":
            r.pop("ringing", None)
        elif etype == "announcement":
            r["announcement"] = bool(data.get("active"))
        elif etype == "night":
            r["night"] = bool(data.get("on"))
        self.push_state()

    # ---------- protocol ----------
    def send(self, t: int, payload: bytes = b""):
        if self.writer is None:
            return
        self.stats[f"tx {NAMES.get(t, hex(t))}"] += 1
        if t not in QUIET:
            self.log("tx", NAMES.get(t, hex(t)), json.loads(payload) if payload else None)
        for i in range(0, max(len(payload), 1), MAX_PAYLOAD):
            part = payload[i:i + MAX_PAYLOAD]
            self.writer.write(HEADER.pack(t, len(part)) + part)

    def send_json(self, t: int, obj: dict):
        self.send(t, json.dumps(obj).encode())

    def send_audio(self, t: int, pcm: bytes):
        for i in range(0, len(pcm), MAX_PAYLOAD):
            self.send(t, pcm[i:i + MAX_PAYLOAD])

    def hello(self):
        self.send_json(HELLO, {
            "protocol": 2, "name": self.cfg["name"], "mac": self.cfg["mac"], "model": "linux",
            "firmware": "mac-satellite 0.2", "sample_rate": RATE, "channels": 1, "bits_per_sample": 16,
            "preroll_ms": PREROLL_MS, "output_buffer_bytes": 1 << 20, "aec": False,
            "listen": True, "monitor": True,
        })

    def start_call(self, wake_word: str, preroll: bytes = b"", prob=None):
        if self.writer is None:
            self.log("info", "no_app")
            return
        if self.in_call:
            return
        self.in_call = True
        self.call_started = time.time()
        self.call_wake = wake_word
        self.dropping_out = False
        self.ring = {k: v for k, v in self.ring.items() if k in ("timers", "night", "ringing")}
        self.ring["phase"] = "wake"
        self.ring["phase_at"] = time.time()
        msg = {"wake_word": wake_word, "preroll_bytes": len(preroll), "aec_preroll_bytes": 0}
        if prob is not None:
            p = int(min(prob, 1) * 255)
            msg.update(probability=p, max_probability=int(min(self.peak, 1) * 255),
                       cutoff=int(self.cutoff * 255))
        self.send_json(CALL_START, msg)
        if preroll:
            self.send_audio(AUDIO_IN, preroll)
        self.push_state()

    def end_call(self, reason: str | None, by_app: bool):
        if not self.in_call:
            return
        self.in_call = False
        if not by_app:
            self.send_json(CALL_END, {"reason": reason})
            if reason == "cancelled":
                self.dropping_out = True
                self.player.clear()
        self.wake.reset()
        self.refractory_until = time.time() + 1.0
        self.push_state()

    async def handle(self, t: int, payload: bytes):
        name = NAMES.get(t, hex(t))
        self.stats[f"rx {name}"] += 1
        data = None
        if t not in (AUDIO_OUT, MODEL_DATA):
            try:
                data = json.loads(payload) if payload else {}
            except ValueError:
                data = {"raw": payload[:64].hex()}
        if t not in QUIET:
            self.log("rx", name, data)

        if t == AUDIO_OUT:
            if not self.dropping_out:
                self.out_ended = False
                self.player.write(payload)
        elif t == AUDIO_OUT_END:
            self.dropping_out = False
            self.out_ended = True
        elif t == EVENT:
            self.ring_event(data.get("type", ""), data.get("data") or {})
        elif t == HANGUP:
            self.end_call(data.get("reason"), by_app=True)
            if data.get("listen") and not self.cfg["muted"]:
                self.start_call("follow_up")
        elif t == MONITOR:
            self.monitor = bool(data.get("active"))
            self.push_state()
        elif t == MODEL_CLEAR:
            pass  # we run our own wake word (no wake_format)
        elif t == MODEL_END:
            self.send_json(0x07, {"ok": False, "id": data.get("id"), "models": 0,
                                  "error": "mac-satellite runs its own wake words"})

    async def on_app(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        if self.writer is not None:
            self.log("warn", "second_conn", peer=f"{peer[0]}:{peer[1]}")
            writer.close()
            return
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.writer, self.app_peer = writer, f"{peer[0]}:{peer[1]}"
        self.log("info", "app_connected", peer=self.app_peer)
        self.hello()
        self.push_state()
        try:
            while True:
                hdr = await reader.readexactly(3)
                t, n = HEADER.unpack(hdr)
                payload = await reader.readexactly(n) if n else b""
                await self.handle(t, payload)
        except (asyncio.IncompleteReadError, ConnectionError) as e:
            self.log("info", "app_disconnected", reason=type(e).__name__)
        finally:
            self.in_call = False
            self.monitor = False
            self.writer = None
            self.app_peer = None
            self.player.clear()
            writer.close()
            self.ring = {}
            self.push_state()

    # ---------- audio pipeline ----------
    def _mic_cb(self, indata, frames, t, status):
        self.loop.call_soon_threadsafe(self.mic_q.put_nowait, bytes(indata))

    async def mic_task(self):
        last_report = 0.0
        last_status = 0.0
        last_level = 0.0
        with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK,
                               device=self.args.input, callback=self._mic_cb):
            self.log("info", "mic_open", name=sd.query_devices(self.args.input or sd.default.device[0])["name"])
            while True:
                chunk = await self.mic_q.get()
                now = time.time()
                samples = np.frombuffer(chunk, dtype=np.int16)
                rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) / 32768
                self.level = max(rms, self.level * 0.9)
                muted = self.cfg["muted"]
                if muted:
                    chunk = b"\x00" * len(chunk)

                if self.in_call:
                    self.send(AUDIO_IN, chunk)
                    if now - self.call_started > CALL_TIMEOUT:
                        self.log("warn", "call_timeout")
                        self.end_call("timeout", by_app=False)
                    if now - last_status > 0.2:
                        last_status = now
                        q = self.player.queued
                        self.send_json(STATUS, {"free_bytes": (1 << 20) - q, "queued_bytes": q,
                                                "queued_ms": q // 32, "speaker_busy": q > 0,
                                                "mic_dropped_bytes": 0, "aec_dropped_bytes": 0})
                else:
                    self.preroll.append(chunk)
                    if self.monitor:
                        self.send(AUDIO_MONITOR, chunk)
                    if not muted:
                        self._wake(chunk, now)
                    if self.monitor and now - last_report > 0.1:
                        last_report = now
                        self.send_json(WAKE_REPORT, {"models": [{
                            "id": self.cfg["wake_word"], "vad": False, "cutoff": int(self.cutoff * 255),
                            "peak": int(self.peak * 255), "window": int(self.prob * 255)}], "detected": []})

                # playback finished?
                if self.out_ended and self.player.queued == 0:
                    self.out_ended = False
                    self.send(PLAYBACK_DRAINED)
                    self.push_state()

                if self.writer is not None:
                    try:
                        await asyncio.wait_for(self.writer.drain(), 1)
                    except Exception:
                        pass

                if now - last_level > 0.05:
                    last_level = now
                    self.push({"meter": {"level": self.level, "prob": self.prob, "cutoff": self.cutoff,
                                         "playing": self.player.queued > 0}})

    def _wake(self, chunk: bytes, now: float):
        # half duplex: no wake word while playing (our own voice)
        if self.player.queued > 0 or now < self.refractory_until:
            return
        for feats in self.features.process_streaming(chunk):
            p = self.wake.process_streaming_prob(feats)
            if p is None:
                continue
            self.prob = p
            self.peak = max(self.peak * 0.97, p)
            if p > self.cutoff:
                if self.monitor:
                    self.log("info", "wake_monitor", p=f"{p:.2f}")
                    self.refractory_until = now + 1
                    return
                self.log("wake", "wake", name=WAKE_NAMES[self.cfg["wake_word"]], p=f"{p:.2f}")
                self.refractory_until = now + 2.0
                self.wake.reset()
                self.start_call(self.cfg["wake_word"], b"".join(self.preroll), prob=p)
                self.preroll.clear()
                self.prob = 0
                return

    # ---------- GUI server ----------
    async def ws_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.gui.add(ws)
        await ws.send_json(self.state())
        for e in self.logbuf:
            await ws.send_json({"log": e})
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            cmd = json.loads(msg.data)
            a = cmd.get("action")
            if a == "mute":
                self.cfg["muted"] = not self.cfg["muted"]
                self.log("info", "muted" if self.cfg["muted"] else "unmuted")
                if self.cfg["muted"] and self.in_call:
                    self.end_call("cancelled", by_app=False)
            elif a == "talk":
                if self.in_call:
                    self.log("info", "button_cancel")
                    self.end_call("cancelled", by_app=False)
                elif self.ring.get("ringing"):
                    self.send_json(DEVICE_EVENT, {"type": "stop_timer"})
                else:
                    self.start_call("manual")
            elif a == "wake_word" and cmd.get("value") in WAKE_NAMES:
                self.cfg["wake_word"] = cmd["value"]
                self.cfg.pop("cutoff", None)
                self._load_wake()
                self.log("info", "wake_word_set", name=WAKE_NAMES[cmd["value"]])
            elif a == "cutoff":
                self.cfg["cutoff"] = self.cutoff = float(cmd["value"])
            elif a == "test_tone":
                t = np.arange(int(RATE * 0.3)) / RATE
                self.player.write((np.sin(2 * np.pi * 880 * t) * 8000).astype(np.int16).tobytes())
            CONFIG.write_text(json.dumps(self.cfg, indent=2))
            self.push_state()
        self.gui.discard(ws)
        return ws

    def check_routes(self, ip: str):
        """Warn when a VPN client routes single LAN hosts (/32) through a tunnel:
        replies to the app would then leave on the wrong interface."""
        import subprocess
        prefix = ip.rsplit(".", 1)[0] + "."
        try:
            out = subprocess.run(["netstat", "-rn", "-f", "inet"], capture_output=True, text=True).stdout
        except OSError:
            return
        hosts = sorted({l.split()[0].split("/")[0] for l in out.splitlines()
                        if l.startswith(prefix) and "/32" in l.split()[0] and "utun" in l})
        if hosts:
            self.log("warn", "vpn_routes", hosts=", ".join(hosts))

    async def run(self):
        self.loop = asyncio.get_running_loop()
        server = await asyncio.start_server(self.on_app, "0.0.0.0", self.args.port)
        ip = self.args.ip or local_ip()
        self.log("info", "listening", addr=f"{ip}:{self.args.port}", name=self.cfg["name"], mac=self.cfg["mac"])

        azc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        props = {"mac": self.cfg["mac"], "protocol": "2"}
        infos = [AsyncServiceInfo(st, f"{self.cfg['name']}.{st}", addresses=[socket.inet_aton(ip)],
                                  port=self.args.port, properties=props, server=f"{self.cfg['name']}.local.")
                 for st in ("_voice-incubator._tcp.local.",)]
        for info in infos:
            await azc.async_register_service(info)
        self.log("info", "mdns")
        self.check_routes(ip)

        app = web.Application()
        app.router.add_get("/", lambda r: web.FileResponse(HERE / "gui.html"))
        app.router.add_get("/ws", self.ws_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", self.args.gui_port).start()
        url = f"http://127.0.0.1:{self.args.gui_port}/"
        LOG.info("GUI: %s", url)
        if not self.args.no_browser:
            webbrowser.open(url)
        try:
            await asyncio.gather(server.serve_forever(), self.mic_task())
        finally:
            for info in infos:
                await azc.async_unregister_service(info)
            await azc.async_close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=6055)
    ap.add_argument("--gui-port", type=int, default=8765)
    ap.add_argument("--ip", help="IP to announce over mDNS (default: auto)")
    ap.add_argument("--input", type=int, help="microphone device index (see --list-devices)")
    ap.add_argument("--output", type=int, help="speaker device index")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    if args.list_devices:
        print(sd.query_devices())
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        asyncio.run(Satellite(args).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
