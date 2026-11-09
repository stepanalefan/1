"""Video (H.264) and audio (Opus) for RemoteDesk, using PyAV (FFmpeg) and soundcard.

Everything is imported lazily: without these packages the app still works with
JPEG tiles and without sound.
"""
import logging
import queue
import sys
import threading
import time
from fractions import Fraction

log = logging.getLogger("media")

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_SAMPLES = 960            # 20 ms


def _why(e):
    """Readable reason for a sound-device error (some backends raise without a message)."""
    msg = str(e).strip()
    return msg if msg else "no sound device was found"


def _com_init():
    """soundcard uses WASAPI (COM) on Windows; every thread that uses it needs this."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception:
            pass


# ------------------------------------------------------------------ video
class VideoEncoder:
    """Screen images -> H.264 access units (Annex B), tuned for low latency."""

    @staticmethod
    def even(w, h):
        return w - w % 2, h - h % 2

    def __init__(self, w, h, fps, quality):
        import av
        self.size = self.even(w, h)
        self.quality = quality
        self.ctx = av.CodecContext.create("libx264", "w")
        self.ctx.width, self.ctx.height = self.size
        self.ctx.pix_fmt = "yuv420p"
        self.ctx.time_base = Fraction(1, 1000)
        self.ctx.framerate = Fraction(max(1, int(fps)), 1)
        pixels = self.size[0] * self.size[1]
        base = 5_000_000 if quality >= 80 else 2_500_000 if quality >= 50 else 1_000_000
        self.ctx.bit_rate = int(base * max(0.4, pixels / (1920 * 1080)))
        self.ctx.gop_size = 10 * 60 * max(1, int(fps))        # keyframes only when needed
        # without global headers x264 repeats SPS/PPS with every keyframe, so a
        # viewer can start decoding at any keyframe
        self.ctx.options = {"preset": "ultrafast", "tune": "zerolatency", "forced-idr": "1"}
        self.ctx.open()
        self._t0 = time.time()
        self._av = av

    def encode(self, img, keyframe=False):
        """img: PIL RGB image. Returns [(is_keyframe, bytes)]."""
        if img.size != self.size:
            img = img.crop((0, 0) + self.size)
        f = self._av.VideoFrame.from_image(img).reformat(format="yuv420p")
        f.pts = int((time.time() - self._t0) * 1000)
        if keyframe:
            f.pict_type = self._av.video.frame.PictureType.I
        return [(bool(p.is_keyframe), bytes(p)) for p in self.ctx.encode(f)]

    def close(self):
        try:
            self.ctx.close()
        except Exception:
            pass


class VideoDecoder:
    """H.264 access units -> RGB numpy arrays."""

    def __init__(self):
        import av
        self._av = av
        self.ctx = av.CodecContext.create("h264", "r")

    def decode(self, data):
        out = None
        for f in self.ctx.decode(self._av.Packet(data)):
            out = f.to_ndarray(format="rgb24")
        return out


# ------------------------------------------------------------------ audio
class AudioCapture:
    """Records what this computer plays (loopback) and encodes it with Opus.
    Calls on_packet(bytes) for every 20 ms packet; on_error(str) if it cannot."""

    def __init__(self, on_packet, on_error):
        self.on_packet, self.on_error = on_packet, on_error
        self.running = True
        threading.Thread(target=self._run, daemon=True, name="rd-audio-in").start()

    def stop(self):
        self.running = False

    def _run(self):
        _com_init()
        try:
            import av
            import numpy as np
            import soundcard as sc
            speaker = sc.default_speaker()
            mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            enc = av.CodecContext.create("libopus", "w")
            enc.sample_rate, enc.layout, enc.format = SAMPLE_RATE, "stereo", "s16"
            enc.bit_rate = 64000
            enc.open()
        except Exception as e:
            log.warning("audio capture unavailable: %s", e)
            self.on_error(f"Sound is not available on the remote computer: {_why(e)}")
            return
        pts = 0
        try:
            with mic.recorder(samplerate=SAMPLE_RATE, channels=CHANNELS, blocksize=FRAME_SAMPLES) as rec:
                while self.running:
                    data = rec.record(numframes=FRAME_SAMPLES)          # float32 (n, ch)
                    if data.ndim == 1:
                        data = data[:, None]
                    if data.shape[1] == 1:
                        data = np.repeat(data, 2, axis=1)
                    pcm = (np.clip(data[:, :2], -1, 1) * 32767).astype(np.int16)
                    fr = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="stereo")
                    fr.sample_rate = SAMPLE_RATE
                    fr.pts = pts
                    pts += pcm.shape[0]
                    for p in enc.encode(fr):
                        self.on_packet(bytes(p))
        except Exception as e:
            if self.running:
                log.warning("audio capture stopped: %s", e)
                self.on_error(f"Sound capture stopped: {_why(e)}")


class AudioPlayer:
    """Decodes Opus packets and plays them on the default speaker.
    Keeps latency low by dropping audio when more than ~200 ms is queued."""

    def __init__(self, on_error=None):
        self.q = queue.Queue(maxsize=50)
        self.on_error = on_error or (lambda m: None)
        self.running = True
        threading.Thread(target=self._run, daemon=True, name="rd-audio-out").start()

    def push(self, packet):
        if self.q.qsize() > 10:            # too far behind: drop to catch up
            try:
                while self.q.qsize() > 2:
                    self.q.get_nowait()
            except queue.Empty:
                pass
        try:
            self.q.put_nowait(packet)
        except queue.Full:
            pass

    def stop(self):
        self.running = False
        self.push(b"")

    def _run(self):
        _com_init()
        try:
            import av
            import numpy as np
            import soundcard as sc
            dec = av.CodecContext.create("opus", "r")
            dec.sample_rate, dec.layout = SAMPLE_RATE, "stereo"
            speaker = sc.default_speaker()
        except Exception as e:
            self.on_error(f"Cannot play sound on this computer: {_why(e)}")
            return
        try:
            with speaker.player(samplerate=SAMPLE_RATE, channels=CHANNELS, blocksize=FRAME_SAMPLES) as out:
                while self.running:
                    pkt = self.q.get()
                    if not pkt or not self.running:
                        continue
                    for f in dec.decode(av.Packet(pkt)):
                        a = f.to_ndarray()                     # planar float (ch, n) or packed
                        if f.format.is_planar:
                            a = a.T
                        else:
                            a = a.reshape(-1, CHANNELS)
                        out.play(np.ascontiguousarray(a, dtype=np.float32))
        except Exception as e:
            if self.running:
                self.on_error(f"Sound playback stopped: {_why(e)}")
