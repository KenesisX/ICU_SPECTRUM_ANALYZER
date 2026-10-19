#!/usr/bin/env python3
"""
sdr_bridge.py
=============

Streams real FFT spectrum data AND real demodulated audio from an SDR
you own to spectrum_analyzer.html over a local WebSocket, and plays
that demodulated audio out your speakers.

Supported hardware (via SoapySDR, one driver interface for all of
them): RTL-SDR, HackRF One, PlutoSDR, LimeSDR, bladeRF, and anything
else with a SoapySDR driver installed. Falls back to pyrtlsdr directly
if SoapySDR isn't installed and you're only using an RTL-SDR.

What this does:
  - Reads raw IQ samples from your SDR
  - Computes an FFT for the spectrum/waterfall display
  - Demodulates the signal at your tuned center frequency using a
    real DSP chain (AM envelope detection, FM quadrature discriminator,
    USB/LSB single-sideband via the phasing method, or CW/narrowband
    tone extraction)
  - Plays the demodulated audio out your speakers
  - Streams spectrum bins + the active demod mode to the browser page

What this does NOT do:
  - No 802.11/Bluetooth packet parsing, no MAC address extraction, no
    device identification. This is audio demodulation of a single
    tuned frequency, the same as tuning a radio — not a network
    sniffer.

--------------------------------------------------------------------
SETUP (RTL-SDR path, no SoapySDR needed)
--------------------------------------------------------------------

    pip install websockets numpy sounddevice pyrtlsdr

    python3 sdr_bridge.py --backend rtlsdr --center 100.1 --span 0.2 --mode fm

(100.1 MHz FM broadcast is a good first test signal if you're not
sure what's on air near you.)

--------------------------------------------------------------------
SETUP (SoapySDR path — HackRF, PlutoSDR, LimeSDR, bladeRF, etc.)
--------------------------------------------------------------------

    # Install SoapySDR + the module for your specific device, e.g.:
    #   macOS:  brew install soapysdr soapyrtlsdr soapyhackrf soapyplutosdr soapylms7
    #   Linux:  apt install soapysdr-tools soapysdr-module-rtlsdr \
    #                        soapysdr-module-hackrf soapysdr-module-plutosdr \
    #                        soapysdr-module-lms7
    pip install websockets numpy sounddevice SoapySDR

    # list what SoapySDR can see:
    python3 sdr_bridge.py --list-devices

    python3 sdr_bridge.py --backend soapy --driver hackrf \
        --center 100.1 --span 0.2 --mode fm

    python3 sdr_bridge.py --backend soapy --driver plutosdr \
        --center 433.9 --span 0.05 --mode am

--------------------------------------------------------------------
MODES
--------------------------------------------------------------------
  --mode am    Envelope detection (AM broadcast, aircraft AM voice)
  --mode fm    Quadrature discriminator (FM broadcast, NFM voice)
  --mode usb   Upper sideband (phasing method)
  --mode lsb   Lower sideband (phasing method)
  --mode cw    Narrow filter + beat-frequency tone (morse/carrier)

--------------------------------------------------------------------
Then open spectrum_analyzer.html, connect to ws://localhost:8765,
and you'll see real spectrum + hear real demodulated audio.
--------------------------------------------------------------------
"""

import argparse
import asyncio
import json
import sys
import threading
import time

try:
    import numpy as np
except ImportError:
    print("Missing dependency: pip install numpy", file=sys.stderr)
    sys.exit(1)

try:
    import websockets
except ImportError:
    print("Missing dependency: pip install websockets", file=sys.stderr)
    sys.exit(1)

try:
    import sounddevice as sd
    HAVE_AUDIO = True
except ImportError:
    HAVE_AUDIO = False


AUDIO_RATE = 48000
NUM_BINS = 512


# ======================================================================
# Shared state
# ======================================================================
class SpectrumState:
    def __init__(self, center_mhz, span_mhz, mode):
        self.center_mhz = center_mhz
        self.span_mhz = span_mhz
        self.mode = mode
        self.bins = None
        self.lock = threading.Lock()

    def update_bins(self, bins_dbm):
        with self.lock:
            self.bins = bins_dbm

    def snapshot(self):
        with self.lock:
            if self.bins is None:
                return None
            return {
                "bins": self.bins,
                "centerMHz": self.center_mhz,
                "spanMHz": self.span_mhz,
                "mode": self.mode,
            }


# ======================================================================
# Demodulators — each takes a block of complex IQ samples at the SDR's
# sample rate and returns real-valued audio samples at some rate you
# then resample to AUDIO_RATE.
# ======================================================================
def demod_am(iq):
    """Envelope detection: audio = magnitude of the analytic signal,
    DC-removed."""
    mag = np.abs(iq)
    mag -= np.mean(mag)
    return mag


def demod_fm(iq, prev_sample_holder):
    """Quadrature discriminator: instantaneous frequency = derivative
    of the phase of the IQ signal."""
    if prev_sample_holder[0] is not None:
        iq_full = np.concatenate(([prev_sample_holder[0]], iq))
    else:
        iq_full = np.concatenate(([iq[0]], iq))
    prev_sample_holder[0] = iq[-1]
    # phase difference between consecutive samples
    diff = iq_full[1:] * np.conj(iq_full[:-1])
    audio = np.angle(diff)
    return audio


def demod_ssb(iq, sample_rate, usb=True):
    """Phasing-method single sideband demod: shift so the wanted
    sideband lands at baseband, then take the real part."""
    n = len(iq)
    shift = np.exp(-1j * 2 * np.pi * (1 if usb else -1) * 1500 * np.arange(n) / sample_rate)
    shifted = iq * shift
    return np.real(shifted)


def demod_cw(iq, sample_rate):
    """Narrowband tone extraction: mix down slightly and take the
    real part, giving an audible beat tone when a carrier is present."""
    n = len(iq)
    shift = np.exp(-1j * 2 * np.pi * 700 * np.arange(n) / sample_rate)
    shifted = iq * shift
    return np.real(shifted)


def resample_audio(audio, src_rate, dst_rate):
    if src_rate == dst_rate or len(audio) == 0:
        return audio
    n_out = int(len(audio) * dst_rate / src_rate)
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    x_old = np.linspace(0, 1, len(audio))
    x_new = np.linspace(0, 1, n_out)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def normalize_audio(audio):
    if len(audio) == 0:
        return audio
    peak = np.max(np.abs(audio))
    if peak < 1e-6:
        return audio
    return (audio / peak * 0.6).astype(np.float32)


# ======================================================================
# Audio output — a simple ring buffer fed by the SDR thread, drained
# by a sounddevice output stream.
# ======================================================================
class AudioPlayer:
    def __init__(self, rate):
        self.rate = rate
        self.buffer = np.zeros(0, dtype=np.float32)
        self.lock = threading.Lock()
        self.stream = None
        if HAVE_AUDIO:
            self.stream = sd.OutputStream(
                samplerate=rate, channels=1, dtype="float32",
                callback=self._callback,
            )
            self.stream.start()

    def push(self, audio):
        if not HAVE_AUDIO:
            return
        with self.lock:
            self.buffer = np.concatenate([self.buffer, audio])
            # cap buffer so latency doesn't grow unbounded
            max_len = self.rate * 2
            if len(self.buffer) > max_len:
                self.buffer = self.buffer[-max_len:]

    def _callback(self, outdata, frames, time_info, status):
        with self.lock:
            if len(self.buffer) >= frames:
                chunk = self.buffer[:frames]
                self.buffer = self.buffer[frames:]
            else:
                chunk = np.concatenate(
                    [self.buffer, np.zeros(frames - len(self.buffer), dtype=np.float32)]
                )
                self.buffer = np.zeros(0, dtype=np.float32)
        outdata[:, 0] = chunk


# ======================================================================
# Shared per-block processing: FFT for display + demod for audio
# ======================================================================
def process_block(iq, sample_rate, mode, fm_state, audio_player):
    # --- spectrum ---
    window = np.hanning(len(iq))
    spectrum = np.fft.fftshift(np.fft.fft(iq * window))
    power_db = 20 * np.log10(np.abs(spectrum) + 1e-9)
    resampled = np.interp(
        np.linspace(0, len(power_db) - 1, NUM_BINS),
        np.arange(len(power_db)),
        power_db,
    )

    # --- demod ---
    if mode == "am":
        audio = demod_am(iq)
    elif mode == "fm":
        audio = demod_fm(iq, fm_state)
    elif mode == "usb":
        audio = demod_ssb(iq, sample_rate, usb=True)
    elif mode == "lsb":
        audio = demod_ssb(iq, sample_rate, usb=False)
    elif mode == "cw":
        audio = demod_cw(iq, sample_rate)
    else:
        audio = np.zeros(len(iq))

    audio = normalize_audio(audio)
    audio = resample_audio(audio, sample_rate, AUDIO_RATE)
    audio_player.push(audio)

    return resampled.tolist()


# ======================================================================
# Backend: pyrtlsdr direct (no SoapySDR needed)
# ======================================================================
def run_rtlsdr_backend(state: SpectrumState, sample_rate_hz, audio_player):
    try:
        from rtlsdr import RtlSdr
    except ImportError:
        print(
            "pyrtlsdr not installed. Run: pip install pyrtlsdr\n"
            "(and install librtlsdr for your OS)",
            file=sys.stderr,
        )
        sys.exit(1)

    sdr = RtlSdr()
    sdr.sample_rate = sample_rate_hz
    sdr.center_freq = state.center_mhz * 1e6
    sdr.gain = "auto"

    print(f"[rtlsdr] {state.center_mhz} MHz, {sample_rate_hz/1e6:.3f} MSPS, mode={state.mode}")

    fm_state = [None]
    block_size = 8192
    try:
        while True:
            samples = sdr.read_samples(block_size)
            bins = process_block(samples, sample_rate_hz, state.mode, fm_state, audio_player)
            state.update_bins(bins)
    finally:
        sdr.close()


# ======================================================================
# Backend: SoapySDR (RTL-SDR, HackRF, PlutoSDR, LimeSDR, bladeRF, ...)
# ======================================================================
def list_soapy_devices():
    try:
        import SoapySDR
    except ImportError:
        print("SoapySDR Python bindings not installed. See --help setup notes.", file=sys.stderr)
        sys.exit(1)
    results = SoapySDR.Device.enumerate()
    if not results:
        print("No SoapySDR devices found.")
        return
    for i, r in enumerate(results):
        print(f"[{i}] {dict(r)}")


def run_soapy_backend(state: SpectrumState, sample_rate_hz, driver, audio_player):
    try:
        import SoapySDR
        from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
    except ImportError:
        print(
            "SoapySDR Python bindings not installed.\n"
            "  macOS: brew install soapysdr <driver-module>, then pip install SoapySDR\n"
            "  Linux: apt install soapysdr-tools soapysdr-module-<driver>, then pip install SoapySDR",
            file=sys.stderr,
        )
        sys.exit(1)

    args = dict(driver=driver) if driver else {}
    dev = SoapySDR.Device(args)
    dev.setSampleRate(SOAPY_SDR_RX, 0, sample_rate_hz)
    dev.setFrequency(SOAPY_SDR_RX, 0, state.center_mhz * 1e6)
    try:
        dev.setGainMode(SOAPY_SDR_RX, 0, True)  # auto gain if supported
    except Exception:
        pass

    rx_stream = dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    dev.activateStream(rx_stream)

    print(f"[soapy:{driver}] {state.center_mhz} MHz, {sample_rate_hz/1e6:.3f} MSPS, mode={state.mode}")

    block_size = 8192
    buff = np.zeros(block_size, dtype=np.complex64)
    fm_state = [None]

    try:
        while True:
            sr = dev.readStream(rx_stream, [buff], block_size)
            if sr.ret <= 0:
                continue
            samples = buff[:sr.ret]
            bins = process_block(samples, sample_rate_hz, state.mode, fm_state, audio_player)
            state.update_bins(bins)
    finally:
        dev.deactivateStream(rx_stream)
        dev.closeStream(rx_stream)


# ======================================================================
# WebSocket server
# ======================================================================
async def ws_handler(websocket):
    print("[ws] client connected")
    try:
        while True:
            await asyncio.sleep(0.05)
            snap = STATE.snapshot()
            if snap is not None:
                await websocket.send(json.dumps(snap))
    except websockets.exceptions.ConnectionClosed:
        print("[ws] client disconnected")


async def main_async(host, port):
    async with websockets.serve(ws_handler, host, port):
        print(f"[ws] serving on ws://{host}:{port}")
        await asyncio.Future()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--backend", choices=["rtlsdr", "soapy"], help="rtlsdr = pyrtlsdr direct; soapy = SoapySDR (HackRF/Pluto/Lime/bladeRF/etc)")
    parser.add_argument("--driver", default=None, help="SoapySDR driver name, e.g. rtlsdr, hackrf, plutosdr, lime, bladerf")
    parser.add_argument("--center", type=float, help="Center frequency in MHz")
    parser.add_argument("--span", type=float, default=0.2, help="Sample-rate-derived span in MHz")
    parser.add_argument("--mode", choices=["am", "fm", "usb", "lsb", "cw"], default="fm")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--list-devices", action="store_true", help="List SoapySDR devices and exit")
    args = parser.parse_args()

    if args.list_devices:
        list_soapy_devices()
        return

    if not args.backend or args.center is None:
        parser.error("--backend and --center are required (unless using --list-devices)")

    if not HAVE_AUDIO:
        print(
            "NOTE: sounddevice not installed (pip install sounddevice) — "
            "spectrum display will work but audio playback is disabled.",
            file=sys.stderr,
        )

    global STATE
    STATE = SpectrumState(args.center, args.span, args.mode)
    audio_player = AudioPlayer(AUDIO_RATE)

    sample_rate_hz = max(0.25, args.span) * 1e6

    if args.backend == "rtlsdr":
        t = threading.Thread(
            target=run_rtlsdr_backend,
            args=(STATE, sample_rate_hz, audio_player),
            daemon=True,
        )
    else:
        driver = args.driver or "rtlsdr"
        t = threading.Thread(
            target=run_soapy_backend,
            args=(STATE, sample_rate_hz, driver, audio_player),
            daemon=True,
        )
    t.start()

    try:
        asyncio.run(main_async(args.host, args.port))
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
