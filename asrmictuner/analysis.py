"""Offline analysis of recorded samples: room tone and voice.

Everything here takes raw int16 audio, shape (frames, channels), and returns
plain JSON-serialisable dicts, so results go straight into the history
database and into an AI prompt without translation.

Thresholds are for speech recognition, not for music or broadcast: what
matters is that speech peaks sit well clear of both the noise floor and full
scale, that nothing tonal (mains hum) sits in the speech band, and that the
signal still has bandwidth above 4 kHz where consonants live.
"""

import math

import numpy as np

from .core import (
    CALIB_TARGET_PEAK_DB, CLIP_LEVEL, CLIP_RUN, FLOOR_DB, FULL_SCALE,
    TARGET_HI_DB, TARGET_LO_DB,
)

BLOCK_S = 0.030                 # analysis block for VAD and level statistics
NFFT = 8192

# Nominal 1/3-octave centres, 31.5 Hz .. 16 kHz.
THIRD_OCTAVES = [
    31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
    1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000,
    12500, 16000,
]

# ASR readiness thresholds.
NOISE_GOOD_DB = -60.0
NOISE_OK_DB = -50.0
SNR_GOOD_DB = 30.0
SNR_OK_DB = 20.0
HUM_WARN_DB = 15.0              # hum line this far above its neighbours
DC_WARN = 0.01                  # 1 % of full scale
IMBALANCE_WARN_DB = 6.0
BANDWIDTH_OK_HZ = 7000.0
DROPOUT_DB = -90.0              # a live mic never gets this quiet: something upstream muted it
DROPOUT_MIN_BLOCKS = 3          # ~90 ms of digital silence counts as one dropout
LEVEL_WINDOW_S = 5.0            # speech level is compared across windows this long
LEVEL_SPREAD_WARN_DB = 6.0
LEVEL_DRIFT_WARN_DB = 4.0


def _db(power_or_mag, power=True, floor=FLOOR_DB):
    value = np.asarray(power_or_mag, dtype=np.float64)
    with np.errstate(divide="ignore"):
        out = (10.0 if power else 20.0) * np.log10(np.maximum(value, 1e-20))
    return np.maximum(out, floor)


def _f(value, digits=2):
    """Round for JSON; None for anything non-finite."""
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(value, digits)


def to_float(audio):
    return audio.astype(np.float64) / FULL_SCALE


def mono(audio):
    """Mean of channels, as float. Only for spectral shape and pitch, where a
    downmix is harmless; levels and clipping are always taken per channel."""
    x = to_float(audio)
    return x.mean(axis=1) if x.ndim == 2 else x


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------

def block_rms_db(x, rate, block_s=BLOCK_S):
    n = max(1, int(rate * block_s))
    count = len(x) // n
    if count == 0:
        return np.array([FLOOR_DB])
    blocks = x[: count * n].reshape(count, n)
    return _db(np.mean(blocks ** 2, axis=1))


def welch_psd(x, rate, nfft=NFFT):
    """Averaged periodogram (Hann, 50 % overlap). Returns (freqs, power)
    with power scaled so a full-scale sine reads about 0 dB at its line."""
    if len(x) < nfft:
        nfft = 1 << max(8, int(math.log2(max(len(x), 256))))
        x = np.pad(x, (0, max(0, nfft - len(x))))
    window = np.hanning(nfft)
    scale = 2.0 / (np.sum(window) ** 2) * 2.0     # one-sided, sine -> 0 dBFS
    step = nfft // 2
    acc = np.zeros(nfft // 2 + 1)
    frames = 0
    for start in range(0, len(x) - nfft + 1, step):
        seg = x[start:start + nfft] * window
        acc += np.abs(np.fft.rfft(seg)) ** 2
        frames += 1
    acc = acc / max(frames, 1) * scale
    freqs = np.fft.rfftfreq(nfft, 1.0 / rate)
    return freqs, acc


def a_weighting_db(freqs):
    f2 = np.asarray(freqs, dtype=np.float64) ** 2
    num = (12194.0 ** 2) * f2 ** 2
    den = (
        (f2 + 20.6 ** 2)
        * np.sqrt((f2 + 107.7 ** 2) * (f2 + 737.9 ** 2))
        * (f2 + 12194.0 ** 2)
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        gain = 20.0 * np.log10(np.maximum(num / np.maximum(den, 1e-30), 1e-30)) + 2.0
    return gain


def third_octave_bands(freqs, power, rate):
    """Band levels in dBFS (sum of power within each 1/3-octave band)."""
    bands = []
    nyquist = rate / 2.0
    for centre in THIRD_OCTAVES:
        lo, hi = centre / 2 ** (1 / 6), centre * 2 ** (1 / 6)
        if lo >= nyquist:
            break
        mask = (freqs >= lo) & (freqs < min(hi, nyquist))
        total = float(np.sum(power[mask])) if mask.any() else 0.0
        bands.append({"hz": centre, "db": _f(_db(total))})
    return bands


def band_energy_db(freqs, power, lo, hi):
    mask = (freqs >= lo) & (freqs < hi)
    return float(_db(np.sum(power[mask]))) if mask.any() else FLOOR_DB


def detect_hum(freqs, power):
    """Mains hum: a line at 50 or 60 Hz (or harmonics) standing clear of its
    neighbourhood. Returns the stronger of the two families."""
    power_db = _db(power)
    best = None
    for mains in (50.0, 60.0):
        lines = []
        for harmonic in range(1, 7):
            target = mains * harmonic
            idx = int(np.argmin(np.abs(freqs - target)))
            lo, hi = max(0, idx - 2), min(len(freqs), idx + 3)
            line = float(np.max(power_db[lo:hi]))
            ring = np.concatenate([
                power_db[max(0, idx - 25): max(0, idx - 6)],
                power_db[idx + 7: idx + 26],
            ])
            if ring.size == 0:
                continue
            prominence = line - float(np.median(ring))
            lines.append({"hz": target, "db": _f(line), "prominence_db": _f(prominence)})
        if not lines:
            continue
        strongest = max(lines, key=lambda l: l["prominence_db"] or -999)
        score = sum(max(0.0, l["prominence_db"] or 0) for l in lines)
        if best is None or score > best["_score"]:
            best = {
                "mains_hz": mains,
                "strongest_hz": strongest["hz"],
                "prominence_db": strongest["prominence_db"],
                "harmonics_present": sum(
                    1 for l in lines if (l["prominence_db"] or 0) >= 10.0
                ),
                "lines": lines,
                "_score": score,
            }
    if best is None:
        return None
    best.pop("_score")
    best["present"] = (best["prominence_db"] or 0) >= HUM_WARN_DB
    return best


def spectral_flatness(power, freqs, lo=100.0, hi=8000.0):
    mask = (freqs >= lo) & (freqs < hi) & (power > 0)
    if not mask.any():
        return None
    p = power[mask]
    return float(np.exp(np.mean(np.log(p))) / np.mean(p))


def spectral_centroid(freqs, power, lo=80.0, hi=8000.0):
    mask = (freqs >= lo) & (freqs < hi)
    total = np.sum(power[mask])
    if total <= 0:
        return None
    return float(np.sum(freqs[mask] * power[mask]) / total)


def bandwidth_hz(freqs, power, drop_db=50.0):
    """Highest frequency whose smoothed level is within `drop_db` of the
    spectrum's peak — a narrowband (Bluetooth HFP, 8 kHz) path shows up as
    a hard cliff at 4 kHz."""
    power_db = _db(power)
    kernel = np.ones(15) / 15
    smooth = np.convolve(power_db, kernel, mode="same")
    mask = freqs >= 100
    if not mask.any():
        return None
    peak = float(np.max(smooth[mask]))
    above = np.where(mask & (smooth >= peak - drop_db))[0]
    return float(freqs[above[-1]]) if above.size else None


def spectral_tilt(freqs, power, lo=300.0, hi=6000.0):
    """Slope in dB per octave of the band-level fit between lo and hi."""
    xs, ys = [], []
    for centre in THIRD_OCTAVES:
        if centre < lo or centre > hi:
            continue
        b_lo, b_hi = centre / 2 ** (1 / 6), centre * 2 ** (1 / 6)
        mask = (freqs >= b_lo) & (freqs < b_hi)
        if not mask.any():
            continue
        xs.append(math.log2(centre))
        ys.append(float(_db(np.sum(power[mask]))))
    if len(xs) < 3:
        return None
    slope, _ = np.polyfit(xs, ys, 1)
    return float(slope)


def pitch_track(x, rate, active_mask, block_s=BLOCK_S):
    """F0 per voiced block by normalised autocorrelation, 60–400 Hz.
    Runs on a 16 kHz decimation, which is ample for fundamental frequency."""
    if rate >= 32000:
        factor = rate // 16000
        x = x[: len(x) // factor * factor].reshape(-1, factor).mean(axis=1)
        rate = rate // factor
    n = int(rate * 0.040)               # 40 ms window, 2+ periods at 60 Hz
    hop = int(rate * block_s)
    lag_min, lag_max = int(rate / 400), int(rate / 60)
    f0s = []
    for i, active in enumerate(active_mask):
        if not active:
            continue
        start = i * hop
        seg = x[start:start + n]
        if len(seg) < n:
            break
        seg = seg - seg.mean()
        energy = np.dot(seg, seg)
        if energy <= 1e-10:
            continue
        corr = np.correlate(seg, seg, mode="full")[n - 1:]
        corr = corr / energy
        window = corr[lag_min:lag_max]
        if window.size == 0:
            continue
        lag = int(np.argmax(window)) + lag_min
        if corr[lag] < 0.5:                 # unvoiced or too noisy to trust
            continue
        # Parabolic interpolation for sub-sample lag.
        if 1 <= lag < len(corr) - 1:
            a, b, c = corr[lag - 1], corr[lag], corr[lag + 1]
            denom = a - 2 * b + c
            if denom != 0:
                lag = lag + 0.5 * (a - c) / denom
        f0s.append(rate / lag)
    return np.array(f0s)


# ---------------------------------------------------------------------------
# per-channel level facts, common to both kinds of sample
# ---------------------------------------------------------------------------

def channel_facts(audio):
    facts = []
    for ch in range(audio.shape[1]):
        data = audio[:, ch].astype(np.int32)
        clipped = np.abs(data) >= CLIP_LEVEL
        runs, longest, current = 0, 0, 0
        if clipped.any():
            for hit in clipped.tolist():
                if hit:
                    current += 1
                    if current == CLIP_RUN:
                        runs += 1
                    longest = max(longest, current)
                else:
                    current = 0
        x = data / FULL_SCALE
        rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
        peak = float(np.max(np.abs(x))) if len(x) else 0.0
        facts.append({
            "rms_dbfs": _f(_db(rms ** 2)),
            "peak_dbfs": _f(_db(peak ** 2)),
            "dc_offset": _f(float(np.mean(x)) if len(x) else 0.0, 5),
            "clipped_samples": int(clipped.sum()),
            "clipped_runs": runs,
            "longest_clip_run": longest,
        })
    imbalance = None
    if len(facts) >= 2:
        levels = [f["rms_dbfs"] for f in facts if f["rms_dbfs"] is not None]
        if len(levels) >= 2:
            imbalance = _f(max(levels) - min(levels))
    return facts, imbalance


# ---------------------------------------------------------------------------
# room tone
# ---------------------------------------------------------------------------

def analyse_room(audio, rate):
    """Characterise a recording of the room with nobody speaking."""
    x = mono(audio)
    channels, imbalance = channel_facts(audio)
    blocks = block_rms_db(x, rate)
    freqs, power = welch_psd(x, rate)
    weighted = power * 10 ** (a_weighting_db(freqs) / 10.0)
    hum = detect_hum(freqs, power)
    median = float(np.median(blocks))
    transients = int(np.sum(blocks > median + 10.0))

    rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
    result = {
        "kind": "room",
        "duration_s": _f(len(x) / rate, 1),
        "rate": rate,
        "channels": channels,
        "channel_imbalance_db": imbalance,
        "noise_rms_dbfs": _f(_db(rms ** 2)),
        "noise_floor_p10_dbfs": _f(np.percentile(blocks, 10)),
        "noise_p90_dbfs": _f(np.percentile(blocks, 90)),
        "noise_a_weighted_dbfs": _f(_db(np.sum(weighted))),
        "stationarity_db": _f(np.std(blocks)),
        "transients": transients,
        "spectral_flatness": _f(spectral_flatness(power, freqs), 3),
        "low_rumble_dbfs": _f(band_energy_db(freqs, power, 20, 150)),
        "speech_band_dbfs": _f(band_energy_db(freqs, power, 300, 3400)),
        "hiss_dbfs": _f(band_energy_db(freqs, power, 4000, 16000)),
        "hum": hum,
        "bands": third_octave_bands(freqs, power, rate),
    }
    result["character"] = describe_room(result)
    result["findings"] = room_findings(result)
    return result


def describe_room(room):
    """One plain-language phrase for what the noise mostly is."""
    parts = []
    hum = room.get("hum") or {}
    if hum.get("present"):
        parts.append(f"mains hum at {hum['strongest_hz']:.0f} Hz")
    rumble = room.get("low_rumble_dbfs") or FLOOR_DB
    speech = room.get("speech_band_dbfs") or FLOOR_DB
    hiss = room.get("hiss_dbfs") or FLOOR_DB
    loudest = max((rumble, "low-frequency rumble (traffic, HVAC, fans)"),
                  (speech, "mid-band noise (voices, TV, appliances)"),
                  (hiss, "broadband hiss (preamp, air)"))
    parts.append(f"mostly {loudest[1]}")
    if (room.get("stationarity_db") or 0) > 4.0 or room.get("transients", 0) > 5:
        parts.append("fluctuating")
    else:
        parts.append("steady")
    return ", ".join(parts)


def room_findings(room):
    findings = []
    floor = room.get("noise_floor_p10_dbfs")
    if floor is not None:
        if floor <= NOISE_GOOD_DB:
            findings.append(("pass", f"Noise floor {floor:.1f} dBFS — quiet."))
        elif floor <= NOISE_OK_DB:
            findings.append(("pass", f"Noise floor {floor:.1f} dBFS — acceptable for dictation."))
        else:
            findings.append(("fail", f"Noise floor {floor:.1f} dBFS is above −50 dBFS. "
                             "Either the gain is too high or the room is loud."))
    hum = room.get("hum") or {}
    if hum.get("present"):
        findings.append(("warn", f"Mains hum at {hum['strongest_hz']:.0f} Hz stands "
                         f"{hum['prominence_db']:.0f} dB above its neighbours "
                         f"({hum['harmonics_present']} harmonics). Check for a ground "
                         "loop, a USB hub or a charger near the mic."))
    if (room.get("stationarity_db") or 0) > 4.0:
        findings.append(("warn", "Noise fluctuates a lot — noise reduction trained on "
                         "this sample will be less effective."))
    imbalance = room.get("channel_imbalance_db")
    if imbalance is not None and imbalance >= IMBALANCE_WARN_DB:
        findings.append(("warn", f"Channels differ by {imbalance:.1f} dB on room tone — one "
                         "capsule is obstructed, or the array is asymmetric. Worth knowing "
                         "if your dictation app takes one channel only."))
    for ch, facts in enumerate(room.get("channels", [])):
        if abs(facts.get("dc_offset") or 0) >= DC_WARN:
            findings.append(("warn", f"Channel {ch + 1} DC offset "
                             f"{facts['dc_offset'] * 100:.1f} % of full scale."))
        if facts.get("clipped_runs"):
            findings.append(("fail", f"Channel {ch + 1} clipped while the room was "
                             "quiet — the gain is far too high."))
    return [{"level": level, "text": text} for level, text in findings]


# ---------------------------------------------------------------------------
# voice
# ---------------------------------------------------------------------------

def analyse_voice(audio, rate, room=None):
    """Characterise a sample of the user speaking normally.

    If a room profile is supplied, its noise floor is used as the noise
    reference for SNR; otherwise the quietest blocks of the sample itself
    (the pauses between phrases) stand in for it.
    """
    x = mono(audio)
    channels, imbalance = channel_facts(audio)
    blocks = block_rms_db(x, rate)

    if room and room.get("noise_floor_p10_dbfs") is not None:
        noise_ref = float(room["noise_floor_p10_dbfs"])
        noise_source = "room profile"
    else:
        noise_ref = float(np.percentile(blocks, 10))
        noise_source = "pauses in this sample"

    gate = max(noise_ref + 12.0, float(np.percentile(blocks, 30)), -55.0)
    active = blocks >= gate
    speech_ratio = float(np.mean(active)) if active.size else 0.0

    n = max(1, int(rate * BLOCK_S))
    active_samples = np.concatenate(
        [x[i * n:(i + 1) * n] for i in np.where(active)[0]]
    ) if active.any() else x
    freqs, power = welch_psd(active_samples, rate)

    # Peaks per active block, worst channel — what the meters judge on.
    peak_blocks = []
    wide = np.abs(audio.astype(np.int32)).max(axis=1)
    for i in np.where(active)[0]:
        seg = wide[i * n:(i + 1) * n]
        if seg.size:
            peak_blocks.append(float(_db((seg.max() / FULL_SCALE) ** 2)))
    peak_p95 = float(np.percentile(peak_blocks, 95)) if peak_blocks else None
    peak_max = max(peak_blocks) if peak_blocks else None

    speech_rms = float(_db(np.mean(active_samples ** 2))) if active_samples.size else None
    active_levels = blocks[active] if active.any() else blocks
    f0 = pitch_track(x, rate, active)

    # Below 80 Hz: under almost every speaker's F0, so energy here is rumble,
    # proximity effect or plosive wind rather than voice.
    low = band_energy_db(freqs, power, 20, 80)
    body = band_energy_db(freqs, power, 150, 1000)
    presence = band_energy_db(freqs, power, 2000, 5000)
    sibilance = band_energy_db(freqs, power, 5000, 10000)

    # Plosive pops: blocks whose sub-80 Hz energy jumps well above the norm.
    plosives = 0
    if active.any():
        lp = []
        for i in np.where(active)[0]:
            seg = x[i * n:(i + 1) * n]
            if len(seg) < n:
                continue
            spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
            f = np.fft.rfftfreq(len(seg), 1.0 / rate)
            lp.append(float(_db(np.sum(spec[f < 80]) + 1e-20)))
        if lp:
            med = float(np.median(lp))
            plosives = int(np.sum(np.array(lp) > med + 15.0))

    # Dropouts: runs of digital silence between the first and last speech
    # block. A raw mic always carries some noise, so these mean a gate, VAD
    # or buffer underrun upstream is muting audio.
    dropouts, dropout_s = 0, 0.0
    if active.any():
        first, last = np.where(active)[0][[0, -1]]
        run = 0
        for level in blocks[first:last + 1]:
            if level < DROPOUT_DB:
                run += 1
                continue
            if run >= DROPOUT_MIN_BLOCKS:
                dropouts += 1
                dropout_s += run * BLOCK_S
            run = 0

    # Level consistency: speech RMS per window. Spread is how much it wanders,
    # drift is the last third against the first (moving away, leaning back,
    # an AGC upstream pumping).
    per = max(1, int(LEVEL_WINDOW_S / BLOCK_S))
    window_levels = []
    for i in range(0, len(blocks), per):
        sel = blocks[i:i + per][active[i:i + per]]
        if sel.size >= per // 4:
            window_levels.append(float(_db(np.mean(10 ** (sel / 10.0)))))
    spread = drift = None
    if len(window_levels) >= 4:
        spread = float(np.percentile(window_levels, 90) - np.percentile(window_levels, 10))
        third = max(1, len(window_levels) // 3)
        drift = float(np.mean(window_levels[-third:]) - np.mean(window_levels[:third]))

    result = {
        "kind": "voice",
        "duration_s": _f(len(x) / rate, 1),
        "rate": rate,
        "channels": channels,
        "channel_imbalance_db": imbalance,
        "speech_ratio": _f(speech_ratio, 3),
        "speech_rms_dbfs": _f(speech_rms),
        "speech_peak_p95_dbfs": _f(peak_p95),
        "speech_peak_max_dbfs": _f(peak_max),
        "crest_factor_db": _f((peak_p95 - speech_rms) if peak_p95 is not None and speech_rms is not None else None),
        "dynamic_range_db": _f(np.percentile(active_levels, 95) - np.percentile(active_levels, 10)),
        "noise_reference_dbfs": _f(noise_ref),
        "noise_reference_source": noise_source,
        "snr_db": _f((speech_rms - noise_ref) if speech_rms is not None else None),
        "f0_median_hz": _f(np.median(f0)) if f0.size else None,
        "f0_p10_hz": _f(np.percentile(f0, 10)) if f0.size else None,
        "f0_p90_hz": _f(np.percentile(f0, 90)) if f0.size else None,
        "voiced_blocks": int(f0.size),
        "spectral_centroid_hz": _f(spectral_centroid(freqs, power), 0),
        "spectral_tilt_db_per_octave": _f(spectral_tilt(freqs, power)),
        "bandwidth_hz": _f(bandwidth_hz(freqs, power), 0),
        "low_vs_body_db": _f(low - body),
        "presence_vs_body_db": _f(presence - body),
        "sibilance_vs_body_db": _f(sibilance - body),
        "plosive_pops": plosives,
        "dropouts": dropouts,
        "dropout_s": _f(dropout_s),
        "level_spread_db": _f(spread),
        "level_drift_db": _f(drift),
        "bands": third_octave_bands(freqs, power, rate),
    }
    result["findings"] = voice_findings(result)
    result["readiness"] = readiness(result)
    return result


def voice_findings(v):
    out = []
    peak = v.get("speech_peak_p95_dbfs")
    if peak is not None:
        if TARGET_LO_DB <= peak <= TARGET_HI_DB:
            out.append(("pass", f"Speech peaks {peak:.1f} dBFS — inside the −18…−10 target."))
        elif peak < TARGET_LO_DB:
            out.append(("warn", f"Speech peaks {peak:.1f} dBFS — too quiet; raise the gain "
                        f"by about {CALIB_TARGET_PEAK_DB - peak:.0f} dB."))
        else:
            out.append(("fail" if peak > -3 else "warn",
                        f"Speech peaks {peak:.1f} dBFS — too hot; lower the gain by about "
                        f"{peak - CALIB_TARGET_PEAK_DB:.0f} dB."))
    runs = sum(c.get("clipped_runs", 0) for c in v.get("channels", []))
    if runs:
        out.append(("fail", f"{runs} clipped runs while speaking. This damage is "
                    "analog — nothing downstream can repair it."))
    snr = v.get("snr_db")
    if snr is not None:
        if snr >= SNR_GOOD_DB:
            out.append(("pass", f"SNR {snr:.0f} dB — excellent separation from the room."))
        elif snr >= SNR_OK_DB:
            out.append(("pass", f"SNR {snr:.0f} dB — good enough for dictation."))
        else:
            out.append(("fail", f"SNR {snr:.0f} dB — speech is too close to the room "
                        "noise. Gain moves both together; get closer to the mic or "
                        "quieten the room."))
    bw = v.get("bandwidth_hz")
    if bw is not None and bw < BANDWIDTH_OK_HZ:
        out.append(("warn", f"Usable bandwidth ends near {bw / 1000:.1f} kHz. "
                    "A Bluetooth headset in hands-free mode, or heavy filtering, "
                    "strips consonant detail ASR relies on."))
    if (v.get("low_vs_body_db") or -99) > -6:
        out.append(("warn", "Strong low-frequency energy — proximity effect or desk "
                    "rumble. A high-pass at ~80 Hz in your ASR chain would help."))
    if v.get("plosive_pops", 0) >= 5:
        out.append(("warn", f"{v['plosive_pops']} plosive pops — angle the mic off-axis "
                    "or use a pop filter."))
    if (v.get("sibilance_vs_body_db") or -99) > -3:
        out.append(("warn", "Harsh sibilance relative to the voice body."))
    imb = v.get("channel_imbalance_db")
    if imb is not None and imb >= IMBALANCE_WARN_DB:
        out.append(("warn", f"Channels differ by {imb:.1f} dB — one capsule is "
                    "obstructed or the array is asymmetric."))
    if v.get("dropouts"):
        out.append(("fail", f"{v['dropouts']} stretches of digital silence mid-speech "
                    f"({v.get('dropout_s') or 0:.1f} s). A gate or VAD before this point is muting "
                    "audio, or the stream is dropping out. If your transcription app has its own VAD, "
                    "remove the one in the chain: two in series clip word starts."))
    spread = v.get("level_spread_db")
    if spread is not None and spread > LEVEL_SPREAD_WARN_DB:
        out.append(("warn", f"Speech level wanders over {spread:.0f} dB between 5-second stretches. "
                    "Keep a steady distance from the mic; an auto gain in the chain also evens this out."))
    drift = v.get("level_drift_db")
    if drift is not None and abs(drift) > LEVEL_DRIFT_WARN_DB:
        out.append(("warn", f"Level {'fell' if drift < 0 else 'rose'} {abs(drift):.0f} dB from the start "
                    "of the reading to the end: posture or distance changed as you went."))
    if (v.get("speech_ratio") or 0) < 0.3:
        out.append(("warn", "Little speech detected in the sample — measurements "
                    "may be unreliable. Speak continuously while recording."))
    return [{"level": level, "text": text} for level, text in out]


def readiness(v):
    """0–100 score for 'will ASR do well with this'. Deliberately simple and
    explainable: start at 100, subtract for each named problem."""
    score = 100.0
    reasons = []

    def hit(points, why):
        nonlocal score
        if points < 1:
            return
        score -= points
        reasons.append({"points": -round(points), "why": why})

    peak = v.get("speech_peak_p95_dbfs")
    if peak is None:
        hit(60, "no speech detected")
    else:
        if peak > -3:
            hit(35, "speech near full scale")
        elif peak > TARGET_HI_DB:
            hit(min(25, (peak - TARGET_HI_DB) * 3), "speech louder than target")
        elif peak < TARGET_LO_DB:
            hit(min(30, (TARGET_LO_DB - peak) * 2), "speech quieter than target")
    runs = sum(c.get("clipped_runs", 0) for c in v.get("channels", []))
    if runs:
        hit(min(40, 15 + runs * 2), "clipping")
    snr = v.get("snr_db")
    if snr is not None and snr < SNR_GOOD_DB:
        hit(min(35, (SNR_GOOD_DB - snr) * 1.5), "limited SNR")
    bw = v.get("bandwidth_hz")
    if bw is not None and bw < BANDWIDTH_OK_HZ:
        hit(15, "narrow bandwidth")
    noise = v.get("noise_reference_dbfs")
    if noise is not None and noise > NOISE_OK_DB:
        hit(min(20, (noise - NOISE_OK_DB) * 2), "high noise floor")
    if v.get("plosive_pops", 0) >= 5:
        hit(5, "plosives")
    if v.get("dropouts"):
        hit(min(20, 8 + 2 * v["dropouts"]), "dropouts (gate or VAD muting speech)")
    if (v.get("level_spread_db") or 0) > LEVEL_SPREAD_WARN_DB:
        hit(5, "unsteady level")
    score = max(0, min(100, round(score)))
    grade = "excellent" if score >= 85 else "good" if score >= 70 else "fair" if score >= 50 else "poor"
    return {"score": score, "grade": grade, "deductions": reasons}


def suggest_index(curve, current_index, speech_peak):
    """Gain index that would put speech peaks at the target, or None."""
    if curve is None or speech_peak is None:
        return None
    here = curve.db(current_index)
    return curve.index_for_db(here + CALIB_TARGET_PEAK_DB - speech_peak)


# ---------------------------------------------------------------------------
# live helpers (called from the UI at ~10 Hz)
# ---------------------------------------------------------------------------

def live_spectrum(audio, rate, points=96, lo=40.0, hi=None):
    """Log-spaced magnitude spectrum in dBFS for the live view."""
    if audio is None or len(audio) < 1024:
        return None
    x = mono(audio)
    n = 1 << int(math.log2(len(x)))
    x = x[-n:] * np.hanning(n)
    spec = np.abs(np.fft.rfft(x)) ** 2 * (4.0 / (np.sum(np.hanning(n)) ** 2))
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    hi = hi or min(rate / 2.0, 16000.0)
    edges = np.geomspace(lo, hi, points + 1)
    out = []
    for a, b in zip(edges[:-1], edges[1:]):
        mask = (freqs >= a) & (freqs < b)
        if mask.any():
            out.append(float(_db(np.max(spec[mask]))))
        else:
            idx = int(np.argmin(np.abs(freqs - (a + b) / 2)))
            out.append(float(_db(spec[idx])))
    return np.sqrt(edges[:-1] * edges[1:]), np.array(out)
