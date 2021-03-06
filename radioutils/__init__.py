from pathlib import Path
import logging
import numpy as np
import scipy.signal as signal
from typing import Union


class Link:
    def __init__(self, range_m, freq_hz, tx_dbm=None, rx_dbm=None):
        self.range = np.atleast_1d(range_m)
        self.freq = freq_hz
        self.txpwr = tx_dbm
        self.rxsens = rx_dbm
        self.c = 299792458.0  # m/s

    def power_dbm(self):
        return self.txpwr

    def power_watts(self):
        return 10.0 ** ((self.txpwr - 30) / 10.0)

    def freq_mhz(self):
        return self.freq / 1e6

    def freq_ghz(self):
        return self.freq / 1e9

    def fspl(self):
        return 20 * np.log10(4 * np.pi / self.c * self.range * self.freq)

    def linkbudget(self):
        if self.rxsens is not None:
            return self.txpwr - self.fspl() - self.rxsens

    def linkreport(self):
        print(f"link margin {self.linkbudget()} dB ")
        print("based on isotropic 0 dBi gain antennas and:")
        print(f"free space path loss {self.fspl()} dB .")
        print(f"RX sensitivity {self.rxsens:0.1f} dBm")
        print(f"TX power {self.power_watts()} watts")
        print(f"for Range [m]= {self.range}  Frequency [MHz]={self.freq_mhz():0.1f}")


def playaudio(dat: np.ndarray, fs: int, ofn: Path = None):
    """
    playback radar data using PyGame audio
    """
    if dat is None:
        return

    try:
        import pygame
    except ImportError:
        return

    fs = int(fs)
    # %% rearrange sound array to [N,2] for Numpy playback/writing
    if isinstance(dat.dtype, np.int16):
        odtype = dat.dtype
        fnorm = 32768
    elif isinstance(dat.dtype, np.int8):
        odtype = dat.dtype
        fnorm = 128
    elif dat.dtype in ("complex128", "float64"):
        odtype = np.float64
        fnorm = 1.0
    elif dat.dtype in ("complex64", "float32"):
        odtype = np.float32
        fnorm = 1.0
    else:
        raise TypeError(f"unknown input type {dat.dtype}")

    if np.iscomplexobj(dat):
        snd = np.empty((dat.size, 2), dtype=odtype)
        snd[:, 0] = dat.real
        snd[:, 1] = dat.imag
    else:
        snd = dat  # monaural

    snd = snd * fnorm / snd.max()
    # %% optional write wav file
    if ofn:
        ofn = Path(ofn).expanduser()
        if not ofn.is_file():
            import scipy.io.wavfile

            print("writing audio to", ofn)
            scipy.io.wavfile.write(ofn, fs, snd)
        else:
            logging.warning(f"did NOT overwrite existing {ofn}")
    # %% play sound
    if 100e3 > fs > 1e3:
        logging.info("attempting playback")
        Nloop = 0
        if pygame is None:
            logging.info("audio playback disabled due to missing Pygame")
            return

        assert snd.ndim in (1, 2), "mono or stereo Nx2"

        # scale to pygame required int16 format
        fnorm = 32768 / snd.max()
        pygame.mixer.pre_init(fs, size=-16, channels=snd.ndim)
        pygame.mixer.init()
        sound = pygame.sndarray.make_sound((snd * fnorm).astype(np.int16))

        sound.play(loops=Nloop)
    else:
        print(f"skipping playback due to fs={fs} Hz")


def loadbin(fn: Path, fs: int, tlim=None, isamp=None) -> np.ndarray:
    """
    we assume single-precision complex64 floating point data
    Often we load data from GNU Radio in complex64 (what Matlab calls complex float32) format.
    complex64 means single-precision complex floating-point data I + jQ.
    """
    LSAMP = 8  # 8 bytes per single-precision complex

    if fn is None:
        return
    fn = Path(fn).expanduser()

    if fs is None:
        raise ValueError(f"must specify sampling freq. for {fn}")
    # %%
    if isinstance(tlim, (tuple, np.ndarray, list)):
        assert len(tlim) == 2, "specify start and end times"
        startbyte = int(LSAMP * tlim[0] * fs)
        count = int((tlim[1] - tlim[0]) * fs)
    elif isamp is not None:
        assert len(isamp) == 2, "specify start and end sample indices"

        startbyte = LSAMP * isamp[0]

        if isinstance(tlim, (float, int)):  # to start at a particular time
            startbyte += int(LSAMP * tlim * fs)

        count = isamp[1] - isamp[0]
    else:
        startbyte = 0
        count = -1  # count=None is not accepted
    # %%
    assert startbyte % 8 == 0, "must have multiple of 8 bytes or entire file is read incorrectly"

    with fn.open("rb") as f:
        f.seek(startbyte)
        sig = np.fromfile(f, np.complex64, count)

    assert sig.ndim == 1 and np.iscomplexobj(sig), "file read incorrectly"
    assert sig.size > 0, "read past end of file, did you specify incorrect time limits?"

    return sig


def am_demod(
    sig,
    fs: int,
    fsaudio: int,
    fc: float,
    fcutoff: float = 10e3,
    frumble: float = None,
    verbose: bool = False,
) -> np.ndarray:
    """
    Envelope demodulates AM with carrier (DSB or SSB).
    Assumes desired AM signal is centered at zero baseband freq.

    inputs:
    -------,
    sig: downconverted (baseband) signal, normally containing amplitude-modulated information with carrier
    fs: sampling frequency [Hz]
    fsaudio: local sound card sampling frequency for audio playback [Hz]
    fcutoff: cutoff frequency of output lowpass filter [Hz]
    frumble: optional cutoff freq for carrier beating removal [Hz]

    outputs:
    --------
    msg: demodulated audio.

    Reference: https://www.mathworks.com/help/dsp/examples/envelope-detection.html
    """
    if isinstance(sig, Path):
        sig = loadbin(sig, fs)
    # if verbose:
    #     plotraw(sig, fs)

    sig = freq_translate(sig, fc, fs)

    sig = downsample(sig, fs, fsaudio, verbose)
    # reject signals outside our channel bandwidth
    sig = final_filter(sig, fsaudio, fcutoff, ftype="lpf", verbose=verbose)
    # %% ideal diode: half-wave rectifier
    sig = sig ** 2
    # %% optional rumble filter
    sig = final_filter(sig, fsaudio, frumble, ftype="hpf", verbose=verbose)

    return sig


def downsample(sig: np.ndarray, fs: int, fsaudio: int, verbose: bool = False) -> np.ndarray:
    if fs == fsaudio:
        return sig

    decim = int(fs / fsaudio)
    if verbose:
        print("downsampling by factor of", decim)

    dtype = sig.dtype

    sig = signal.decimate(sig, decim, zero_phase=True).astype(dtype)

    return sig


def fm_demod(
    sig, fs: int, fsaudio: int, fc: float, fmdev=75e3, verbose: bool = False
) -> np.ndarray:
    """
    currently this function discards all but the monaural audio.

    fmdev: FM deviation of monaural modulation in Hz  (for scaling)
    """
    if isinstance(sig, Path):
        sig = loadbin(sig, fs)

    sig = freq_translate(sig, fc, fs)
    # %% reject signals outside our channel bandwidth
    sig = final_filter(sig, fs, fmdev * 1.5, ftype="lpf", verbose=verbose)

    # FM is a time integral, angle modulation--so let's undo the FM
    Cfm = fs / (2 * np.sqrt(2) * np.pi * fmdev)  # a scalar constant
    sig = Cfm * np.diff(np.unwrap(np.angle(sig)))

    if verbose:
        from .plots import plot_fmbaseband

        plot_fmbaseband(sig, fs, 100e3)

    # demodulated monoaural_ audio (plain audio waveform)
    # This has to occur AFTER demodulation, since WBFM is often wider than soundcard sample rate!
    m = downsample(sig, fs, fsaudio, verbose)

    return m


def ssb_demod(
    sig: np.ndarray, fs: int, fsaudio: int, fc: float, fcutoff: float = 5e3, verbose: bool = False
) -> np.ndarray:
    """
    filter method SSB/DSB suppressed carrier demodulation

    sig: downconverted (baseband) signal, normally containing amplitude-modulated information
    fs: sampling frequency [Hz]
    fsaudio: local sound card sampling frequency for audio playback [Hz]
    fc: supressed carrier frequency (a priori)
    fcutoff: cutoff frequency of output lowpass filter [Hz]
    """
    if isinstance(sig, Path):
        sig = loadbin(sig, fs)
    # %% assign elapsed time vector
    t = np.arange(0, sig.size / fs, 1 / fs)
    # %% SSB demod
    bx = np.exp(1j * 2 * np.pi * fc * t)
    sig *= bx[: sig.size]  # sometimes length was off by one

    sig = downsample(sig, fs, fsaudio, verbose)

    return sig


def freq_translate(sig: np.ndarray, fc: float, fs: int) -> np.ndarray:
    # %% assign elapsed time vector
    t = np.arange(sig.size) / fs
    # %% frequency translate
    if fc is not None:
        bx = np.exp(1j * 2 * np.pi * fc * t)
        sig *= bx[: sig.size]  # downshifted

    return sig


# def lpf_design(fs:int, fc:float, L:int):


def lpf_design(fs: int, fcutoff, L=50):
    """
    Design FIR low-pass filter coefficients "b"
    fcutoff: cutoff frequency [Hz]
    fs: sampling frequency [Hz]
    L: number of taps (more taps->narrower transition band->more CPU)

    https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.remez.html
    """
    # 0.8*fc is arbitrary, for finite transition width

    # return signal.remez(L, [0, 0.8*fcutoff, fcutoff, 0.5*fs], [1., 0.], Hz=fs)
    return signal.firwin(L, fcutoff, nyq=0.5 * fs, pass_zero=True)


def hpf_design(fs: int, fcutoff: float, L: int = 199):
    """
    Design FIR high-pass filter coefficients "b"
    fcutoff: cutoff frequency [Hz]
    fs: sampling frequency [Hz]
    L: number of taps (more taps->narrower transition band->more CPU)

    https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.remez.html
    """
    # 0.8*fc is arbitrary, for finite transition width

    # return signal.remez(L, [0, 0.8*fcutoff, fcutoff, 0.5*fs], [1., 0.], Hz=fs)
    return signal.firwin(
        L, fcutoff, nyq=0.5 * fs, pass_zero=False, width=10, window="kaiser", scale=True
    )


def bpf_design(fs: int, fcutoff: float, flow: float = 300.0, L: int = 256):
    """
    Design FIR bandpass filter coefficients "b"
    fcutoff: cutoff frequency [Hz]
    fs: sampling frequency [Hz]
    flow: low cutoff freq [Hz] to eliminate rumble or beating carriers
    L: number of taps (more taps->narrower transition band->more CPU)

    https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.remez.html
    """
    firtype = "firwin"

    if firtype == "remez":
        # 0.8*fc is arbitrary, for finite transition width

        b = signal.remez(
            L, [0, 0.8 * flow, flow, 0.8 * fcutoff, fcutoff, 0.5 * fs], [0.0, 1.0, 0.0], Hz=fs
        )
    elif firtype == "firwin":
        b = signal.firwin(
            L,
            [flow, fcutoff],
            pass_zero=False,
            width=100,
            nyq=0.5 * fs,
            window="kaiser",
            scale=True,
        )

    elif firtype == "matlab":
        assert L % 2 != 0, "must have odd number of taps"
        from oct2py import Oct2Py

        with Oct2Py() as oc:
            oc.eval("pkg load signal")
            b = oc.fir1(L + 1, [0.03, 0.35], "bandpass")

    return b


def final_filter(
    sig: np.ndarray, fs: int, fcutoff: Union[None, float], ftype: str, verbose: bool = False
) -> np.ndarray:
    if fcutoff is None:
        return sig

    assert fcutoff < 0.5 * fs, "aliasing due to filter cutoff > 0.5*fs"

    if ftype == "lpf":
        b = lpf_design(fs, fcutoff)
    elif ftype == "bpf":
        b = bpf_design(fs, fcutoff)
    elif ftype == "hpf":
        b = hpf_design(fs, fcutoff)
    else:
        raise ValueError(f"Unknown filter type {ftype}")

    sig = signal.lfilter(b, 1, sig)

    if verbose:
        from .plots import plotfir

        print(ftype, " filter cutoff [Hz] ", fcutoff)
        plotfir(b, fs)

    return sig
