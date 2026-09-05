"""mammon.ui.sounds -- the register's transaction-accepted sound.

Quicken plays a short "ka-ching" when a transaction is accepted, and it is
genuinely useful feedback: entering a register full of transactions is a
heads-down, keyboard-driven task, and the sound confirms the save without
asking the eyes to leave the next row. It is off-switchable from Settings for
anyone who disagrees (ui/prefs.sound_enabled).

Two decisions here are deliberate:

**The sound is synthesized, not shipped.** A .wav in the repository would be a
binary blob nobody can review in a diff, with a licence to keep track of, to
carry roughly a second of two-tone chime. The waveform is generated on first use
and cached next to the database, so it costs one small file per install and
nothing in version control.

**It only plays on a real change.** Sound tied to "an editor closed" fires when
you click a cell and click away, which trains you to stop hearing it -- and a
confirmation you have learned to ignore is worse than none, because it no longer
distinguishes a save from a stray click. RegisterModel emits ``transactionSaved``
only when a write actually altered or created a row, and that is what plays.

Playback degrades quietly: if no audio backend is available, or the device is
busy, the register must not raise or stall. Nothing here is allowed to interrupt
a save.
"""
from __future__ import annotations

import math
import os
import struct
import wave
from pathlib import Path

_CACHED: Path | None = None
_FAILED = False

# Two struck tones a fifth apart, the second landing while the first still rings
# -- a till drawer, roughly. Short enough to finish before the next row is typed.
_SAMPLE_RATE = 22050
_TONES = ((1_318.5, 0.00, 0.28),      # E6
          (1_975.5, 0.06, 0.34))      # B6


def _render_wav(path: Path) -> None:
    """Write the ka-ching to ``path`` as 16-bit mono PCM."""
    total = max(start + dur for _f, start, dur in _TONES)
    frames = int(_SAMPLE_RATE * total)
    samples = [0.0] * frames
    for freq, start, dur in _TONES:
        begin = int(start * _SAMPLE_RATE)
        length = int(dur * _SAMPLE_RATE)
        for i in range(length):
            if begin + i >= frames:
                break
            # Exponential decay: a struck-metal envelope, not an organ note.
            envelope = math.exp(-4.5 * (i / length))
            samples[begin + i] += envelope * math.sin(
                2.0 * math.pi * freq * (i / _SAMPLE_RATE))

    peak = max((abs(s) for s in samples), default=1.0) or 1.0
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SAMPLE_RATE)
        w.writeframes(b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, s / peak)) * 24_000))
            for s in samples))


def sound_path(data_dir=None) -> Path:
    """Path to the cached ka-ching, rendering it if it is not there yet.

    Anchored on the data directory like every other generated file (see
    download_log.default_data_dir), never the current working directory.
    """
    global _CACHED
    if _CACHED is not None and _CACHED.exists() and data_dir is None:
        return _CACHED
    if data_dir is None:
        from mammon import download_log
        data_dir = download_log.default_data_dir()
    path = Path(data_dir) / "accepted.wav"
    if not path.exists():
        _render_wav(path)
    if data_dir is None:
        _CACHED = path
    return path


def play_accepted(enabled: bool = True) -> bool:
    """Play the transaction-accepted sound. Returns True if playback started.

    Never raises: a missing audio backend, a busy device or an unwritable data
    directory must not interrupt saving a transaction. A failure disables further
    attempts for the session rather than retrying on every keystroke.
    """
    global _FAILED
    if not enabled or _FAILED:
        return False
    # A headless run has nobody to hear it. Every Qt test in this repo sets the
    # offscreen platform at module import, and without this guard the suite
    # played the chime once per saved transaction -- hundreds of times, out of
    # any UI. Keying on the platform rather than on a test flag means it cannot
    # be forgotten when a new test file is added.
    if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() == "offscreen":
        return False
    try:
        from PyQt5.QtMultimedia import QSound
        QSound.play(str(sound_path()))
        return True
    except Exception:
        _FAILED = True
        return False


def reset_for_tests() -> None:
    """Forget the cached path and any prior playback failure."""
    global _CACHED, _FAILED
    _CACHED, _FAILED = None, False
