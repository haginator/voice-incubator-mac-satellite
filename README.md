# Voice Incubator Mac Satellite

*[Deutsch weiter unten](#deutsch)*

A virtual satellite for [Voice Incubator](https://github.com/OHF-Voice/voice-incubator)
that runs on your Mac – try the app without flashing a Voice PE.

> [!NOTE]
> Unofficial test client, not affiliated with the Open Home Foundation. Voice
> Incubator itself is experimental; this client may break with protocol changes.

## What it does

- Speaks the satellite protocol 2 (`firmware-voice-incubator/PROTOCOL.md`) as
  `model: "linux"`, announced over mDNS as `_voice-incubator._tcp` on port 6055.
  The app finds it and connects like it would to a Voice PE.
- Microphone → `AUDIO_IN`; the app's answers play on the Mac's speaker.
- Wake word runs locally with [microWakeWord](https://github.com/OHF-Voice/pymicro-wakeword)
  (Okay Nabu, Hey Jarvis, Hey Mycroft, Alexa); the app verifies it a second time.
- Follow-up questions (`HANGUP` with `listen`), the app's Test page (`MONITOR`,
  `WAKE_REPORT`), stopping a ringing timer.
- Web GUI at http://127.0.0.1:8765 (English/German, follows the browser):
  a 12-LED ring showing the app's events and colors (listening, speaker color,
  responding, result, timers, night mode), mute, talk/cancel button (space bar),
  level and wake word meters, threshold, and a live log of every frame.

## Requirements

macOS with Python 3.12 (via [uv](https://github.com/astral-sh/uv)), and the
Voice Incubator app on the same network (it needs mDNS and must reach the Mac
on TCP 6055).

## Usage

```bash
./start.sh                       # creates the venv on first run, opens the GUI
./start.sh --list-devices        # list audio devices
./start.sh --input 2 --output 3  # pick microphone / speaker
./start.sh --ip 192.168.1.50     # IP to announce, if auto-detection picks the wrong one
```

Name, fake MAC (stable, locally administered) and wake word settings live in
`config.json`, created on first start. macOS asks for microphone access once.

## Limitations

- No echo-cancelled second capture (`aec: false`): speaker identification works
  on the raw signal. Half duplex: no wake word while it plays. Headphones help.
- Only the built-in microWakeWord models run; wake words trained or uploaded in
  the app (`mixednet`) are declined.
- Responses are always spoken with the app's Piper: the app uses a Home
  Assistant TTS entity only through a Home Assistant media player, which this
  client doesn't have.

## Troubleshooting

- **The app finds the satellite but logs `Connect failed ([Errno 110])`:** often a
  VPN client on the Mac routing single LAN hosts through its tunnel, so replies
  to the app leave on the wrong interface. The client warns at startup when it
  sees such routes; disconnect the VPN while testing.

---

## Deutsch

Ein virtueller Satellit für [Voice Incubator](https://github.com/OHF-Voice/voice-incubator),
der auf dem Mac läuft – die App ausprobieren, ohne einen Voice PE zu flashen.

> [!NOTE]
> Inoffizieller Test-Client, kein Projekt der Open Home Foundation.

- Spricht das Satelliten-Protokoll 2 als `model: "linux"`, per mDNS
  (`_voice-incubator._tcp`, Port 6055) angekündigt – die App verbindet sich selbst.
- Mikrofon → App, Antworten über den Mac-Lautsprecher.
- Wakeword lokal per microWakeWord (Okay Nabu, Hey Jarvis, Hey Mycroft, Alexa),
  die App prüft es zusätzlich.
- Web-GUI (http://127.0.0.1:8765, Deutsch/Englisch nach Browser-Sprache): LED-Ring
  mit den Farben aus den Events, Mute, Sprechen/Abbrechen (Leertaste), Pegel-
  und Wakeword-Anzeige, Schwelle, Live-Debug aller Frames.

Start: `./start.sh` (legt beim ersten Mal die venv an). Weitere Optionen siehe
oben unter *Usage*.

**Grenzen:** kein AEC-Kanal, Halbduplex (Kopfhörer empfohlen), nur die
eingebauten Wakewords, Antworten immer mit Piper (HA-TTS braucht einen
HA-Mediaplayer).

**Verbindet sich nicht (`Errno 110` im App-Log):** häufig ein VPN-Client auf dem
Mac, der einzelne LAN-Hosts in den Tunnel routet. Der Client warnt beim Start
davor – VPN zum Testen trennen.

## License

[Apache-2.0](LICENSE)
