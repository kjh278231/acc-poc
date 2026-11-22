"""Audio helpers: re-chunking and resampling.

FRAGMENT reassembly moved to the kit's ``a2a_connect.fragments`` in task 3.7;
the bridge no longer keeps its own reassembler. All PCM here is 16-bit signed
little-endian mono. Kept dependency-free (no numpy) so the container stays
small and the unit tests run anywhere.
"""
from __future__ import annotations

import array
from typing import Iterator, Optional


def frame_bytes(sample_rate: int, frame_ms: int) -> int:
    """Bytes in one mono 16-bit frame of ``frame_ms`` at ``sample_rate``.

    e.g. 24 kHz, 100 ms -> 24000 * 0.1 * 2 = 4800 bytes.
    """
    samples = int(sample_rate * frame_ms / 1000)
    return samples * 2


class ReChunker:
    """Buffers a byte stream and yields fixed-size frames.

    Nova emits audio in arbitrary sizes; Connect wants steady 100 ms frames.
    Feed with :meth:`push`; drain the tail at end of turn with :meth:`flush`.
    """

    def __init__(self, frame_size: int) -> None:
        if frame_size <= 0:
            raise ValueError("frame_size must be positive")
        self.frame_size = frame_size
        self._buf = bytearray()

    def push(self, data: bytes) -> Iterator[bytes]:
        self._buf.extend(data)
        while len(self._buf) >= self.frame_size:
            yield bytes(self._buf[: self.frame_size])
            del self._buf[: self.frame_size]

    def flush(self, pad: bool = True) -> Optional[bytes]:
        """Return the remaining partial frame, zero-padded to a full frame.

        Returns ``None`` when nothing is buffered.
        """
        if not self._buf:
            return None
        tail = bytes(self._buf)
        self._buf.clear()
        if pad and len(tail) < self.frame_size:
            tail = tail + b"\x00" * (self.frame_size - len(tail))
        return tail


def upsample_linear(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linearly interpolate 16-bit mono PCM from ``src_rate`` to ``dst_rate``.

    Used only if Nova rejects the negotiated input rate (README V2). Integer
    ratios (8 kHz -> 16 kHz) are the common case and stay exact at the taps.
    """
    if src_rate == dst_rate or not pcm:
        return pcm
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    n_src = len(src)
    if n_src == 0:
        return b""
    n_dst = max(1, round(n_src * dst_rate / src_rate))
    out = array.array("h", bytearray(n_dst * 2))
    for i in range(n_dst):
        pos = i * (n_src - 1) / max(1, n_dst - 1) if n_dst > 1 else 0.0
        lo = int(pos)
        hi = min(lo + 1, n_src - 1)
        frac = pos - lo
        out[i] = int(src[lo] + (src[hi] - src[lo]) * frac)
    return out.tobytes()


def silence_frame(frame_size: int) -> bytes:
    return b"\x00" * frame_size


def wav_to_pcm(path: str) -> "tuple[bytes, int]":
    """Read a mono 16-bit WAV, returning ``(pcm_bytes, sample_rate)``."""
    import wave

    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError("harness WAV must be mono 16-bit PCM")
        return w.readframes(w.getnframes()), w.getframerate()


def pcm_to_wav(path: str, pcm: bytes, sample_rate: int) -> None:
    """Write mono 16-bit PCM to a WAV file (used by the harness to save audio)."""
    import wave

    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
