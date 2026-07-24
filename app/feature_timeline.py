from __future__ import annotations

import array
import cmath
import hashlib
import io
import json
import math
import sys
from dataclasses import dataclass

from .job_repository import ClaimedJob
from .object_store import FileSystemObjectStore
from .processing_worker import TerminalProcessingError, TransientProcessingError


ALGORITHM_VERSION = "sound-visualization-features-v1"
SAMPLE_RATE = 44_100
FRAME_RATE = 10
FFT_SIZE = 2048
SIGNAL_THRESHOLD_DBFS = -48.0


@dataclass(frozen=True, slots=True)
class FeatureTimeline:
    object_key: str
    byte_length: int
    sha256: str
    timeline_id: str
    algorithm_version: str
    frame_rate: int
    frame_count: int
    duration_ms: int


class FeatureTimelineBuilder:
    def __init__(self, object_store: FileSystemObjectStore) -> None:
        self.object_store = object_store

    def build(self, job: ClaimedJob) -> FeatureTimeline:
        metadata_key = f"jobs/{job.job_id}/normalized/audio.json"
        timeline_key = f"jobs/{job.job_id}/features/timeline.json"
        try:
            with self.object_store.open_staging(metadata_key) as source:
                metadata = json.load(source)
            pcm_key = str(metadata["pcm"]["object_key"])
            channels = int(metadata["channels"])
            sample_rate = int(metadata["sample_rate"])
            duration_ms = int(metadata["duration_ms"])
            with self.object_store.open_staging(pcm_key) as source:
                pcm_bytes = source.read()
        except (FileNotFoundError, OSError) as error:
            raise TransientProcessingError(
                "NORMALIZED_PCM_MISSING",
                "归一化 PCM 暂不可用",
            ) from error
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise TerminalProcessingError(
                "NORMALIZED_PCM_METADATA_INVALID",
                "归一化 PCM 元数据无效",
            ) from error

        if sample_rate != SAMPLE_RATE or channels not in {1, 2}:
            raise TerminalProcessingError(
                "NORMALIZED_PCM_PROFILE_INVALID",
                "归一化 PCM 参数无效",
            )
        mono = _decode_pcm_mono(pcm_bytes, channels)
        expected_samples = round(duration_ms * SAMPLE_RATE / 1000)
        if not mono or abs(len(mono) - expected_samples) > 1:
            raise TerminalProcessingError(
                "NORMALIZED_PCM_LENGTH_INVALID",
                "归一化 PCM 时长不一致",
            )

        frames = _extract_features(mono, duration_ms)
        pcm_sha256 = hashlib.sha256(pcm_bytes).hexdigest()
        timeline_id = hashlib.sha256(
            f"{ALGORITHM_VERSION}:{pcm_sha256}".encode("ascii")
        ).hexdigest()
        payload = {
            "schema_version": 1,
            "algorithm_version": ALGORITHM_VERSION,
            "timeline_id": timeline_id,
            "pcm_sha256": pcm_sha256,
            "sample_rate": SAMPLE_RATE,
            "frame_rate": FRAME_RATE,
            "frame_interval_ms": 100,
            "sampling_rule": "frame-start-window-zero-pad",
            "duration_ms": duration_ms,
            "frame_count": len(frames),
            "features": frames,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.object_store.replace_staging(timeline_key, io.BytesIO(encoded))
        return FeatureTimeline(
            object_key=timeline_key,
            byte_length=len(encoded),
            sha256=hashlib.sha256(encoded).hexdigest(),
            timeline_id=timeline_id,
            algorithm_version=ALGORITHM_VERSION,
            frame_rate=FRAME_RATE,
            frame_count=len(frames),
            duration_ms=duration_ms,
        )


def _decode_pcm_mono(pcm: bytes, channels: int) -> tuple[float, ...]:
    frame_bytes = channels * 2
    if not pcm or len(pcm) % frame_bytes:
        raise TerminalProcessingError(
            "NORMALIZED_PCM_INVALID",
            "归一化 PCM 数据损坏",
        )
    values = array.array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    if channels == 1:
        return tuple(value / 32768.0 for value in values)
    return tuple(
        (values[index] + values[index + 1]) / 65536.0
        for index in range(0, len(values), 2)
    )


def _extract_features(samples: tuple[float, ...], duration_ms: int) -> list[dict[str, object]]:
    frame_count = max(1, math.ceil(duration_ms * FRAME_RATE / 1000))
    window = tuple(
        0.5 - 0.5 * math.cos(2 * math.pi * index / (FFT_SIZE - 1))
        for index in range(FFT_SIZE)
    )
    window_gain = sum(window) / 2.0
    previous_spectrum = [0.0] * (FFT_SIZE // 2)
    smoothed = {
        "volume": 0.0,
        "instantVolume": 0.0,
        "bass": 0.0,
        "mid": 0.0,
        "treble": 0.0,
        "spectralFlux": 0.0,
        "transient": 0.0,
        "centroid": 0.35,
        "pitchHz": 220.0,
        "pitchNormalized": 0.45,
        "pitchConfidence": 0.0,
    }
    pitch_history: list[float] = []
    last_stable_pitch_hz = 220.0
    noise_floor_db = -58.0
    flux_floor = 0.015
    last_onset_ms = -1_000_000.0
    result: list[dict[str, object]] = []

    for frame_index in range(frame_count):
        timestamp_ms = frame_index * 100.0
        start = round(frame_index * SAMPLE_RATE / FRAME_RATE)
        time_values = [
            samples[position] if position < len(samples) else 0.0
            for position in range(start, start + FFT_SIZE)
        ]
        mean = sum(time_values) / FFT_SIZE
        centered = [value - mean for value in time_values]
        rms = math.sqrt(sum(value * value for value in centered) / FFT_SIZE)
        peak = max(abs(value) for value in centered)
        level = max(rms, peak * 0.35)
        dbfs = max(-60.0, min(0.0, 20.0 * math.log10(max(level, 1e-8))))
        if dbfs < noise_floor_db + 8:
            noise_floor_db += (dbfs - noise_floor_db) * 0.012
        gate_db = min(-42.0, noise_floor_db + 7)
        gated_db = max(0.0, dbfs - gate_db)
        volume_raw = _soft_limit(_clamp01(gated_db / max(14.0, -gate_db)) * 1.45)
        instant_volume = _soft_limit(
            _smooth_toward(smoothed["instantVolume"], volume_raw, 0.65, 0.28)
        )
        has_signal = dbfs > max(SIGNAL_THRESHOLD_DBFS, gate_db)

        spectrum = _spectrum(centered, window, window_gain)
        bin_hz = SAMPLE_RATE / FFT_SIZE

        def band_energy(low: float, high: float) -> float:
            first = max(0, math.floor(low / bin_hz))
            last = min(len(spectrum) - 1, math.ceil(high / bin_hz))
            if last < first:
                return 0.0
            values = spectrum[first : last + 1]
            return math.sqrt(sum(value * value for value in values) / len(values))

        band_gate = 1.0 if has_signal else 0.0
        bass_raw = _soft_limit(band_energy(45, 250) * band_gate)
        mid_raw = _soft_limit(band_energy(250, 2200) * band_gate)
        treble_raw = _soft_limit(band_energy(2200, 9000) * 1.15 * band_gate)

        denominator = sum(spectrum[1:])
        centroid_hz = (
            sum(index * bin_hz * spectrum[index] for index in range(1, len(spectrum)))
            / denominator
            if denominator > 1e-6
            else 1000.0
        )
        centroid_raw = _clamp01((centroid_hz - 200) / 6000)

        pitch_hz_raw, pitch_confidence_raw = _estimate_spectral_pitch(
            spectrum,
            bin_hz,
        )
        if has_signal and pitch_confidence_raw > 0.65:
            shape_pitch_hz = pitch_hz_raw
            last_stable_pitch_hz = pitch_hz_raw
        elif has_signal:
            shape_pitch_hz = min(1000.0, max(80.0, centroid_hz * 0.22))
            pitch_confidence_raw = max(0.2, pitch_confidence_raw * 0.6)
        else:
            shape_pitch_hz = last_stable_pitch_hz
            pitch_confidence_raw = 0.0
        pitch_history.append(shape_pitch_hz)
        if len(pitch_history) > 5:
            pitch_history.pop(0)
        median_pitch_hz = sorted(pitch_history)[len(pitch_history) // 2]
        pitch_normalized_raw = _normalize_pitch(median_pitch_hz)

        positive_change = sum(
            max(0.0, spectrum[index] - previous_spectrum[index])
            for index in range(1, len(spectrum))
        )
        spectrum_energy = sum(spectrum[1:])
        previous_spectrum[:] = spectrum
        flux_raw = (
            positive_change / max(6.0, math.sqrt(spectrum_energy) * 3.2)
            if has_signal
            else 0.0
        )
        if flux_raw < flux_floor * 2.5:
            flux_floor += (flux_raw - flux_floor) * 0.02
        gated_flux = _clamp01((flux_raw - flux_floor * 1.8) * 7.5)
        onset_ready = timestamp_ms - last_onset_ms > 170
        onset = (
            _clamp01((gated_flux - 0.12) * 2.4)
            if onset_ready and gated_flux > 0.16 and volume_raw > 0.055
            else 0.0
        )
        if onset > 0:
            last_onset_ms = timestamp_ms

        smoothed["volume"] = _soft_limit(
            _attack_release(smoothed["volume"], volume_raw, 100, 60, 360)
        )
        smoothed["instantVolume"] = instant_volume
        smoothed["bass"] = _soft_limit(
            _attack_release(smoothed["bass"], bass_raw, 100, 85, 480)
        )
        smoothed["mid"] = _soft_limit(
            _attack_release(smoothed["mid"], mid_raw, 100, 50, 260)
        )
        smoothed["treble"] = _soft_limit(
            _attack_release(smoothed["treble"], treble_raw, 100, 28, 160)
        )
        smoothed["spectralFlux"] = _soft_limit(
            _attack_release(smoothed["spectralFlux"], gated_flux, 100, 32, 210)
        )
        smoothed["transient"] = (
            onset
            if onset > 0
            else _attack_release(smoothed["transient"], 0, 100, 1, 480)
        )
        smoothed["centroid"] = _clamp01(
            _smooth_toward(smoothed["centroid"], centroid_raw, 0.12, 0.06)
        )
        smoothed["pitchNormalized"] = _clamp01(
            _attack_release(
                smoothed["pitchNormalized"],
                pitch_normalized_raw if has_signal else 0.45,
                100,
                170 if has_signal else 80,
                420 if has_signal else 520,
            )
        )
        smoothed["pitchHz"] = _attack_release(
            smoothed["pitchHz"],
            median_pitch_hz if has_signal else last_stable_pitch_hz,
            100,
            170 if has_signal else 80,
            420 if has_signal else 520,
        )
        smoothed["pitchConfidence"] = _clamp01(
            _attack_release(
                smoothed["pitchConfidence"],
                pitch_confidence_raw if has_signal else 0.0,
                100,
                90,
                380,
            )
        )

        result.append(
            {
                "timestampNanos": frame_index * 100_000_000,
                "volume": _round(smoothed["volume"]),
                "instantVolume": _round(instant_volume),
                "dbfs": _round(dbfs),
                "hasSignal": has_signal,
                "bass": _round(smoothed["bass"]),
                "mid": _round(smoothed["mid"]),
                "treble": _round(smoothed["treble"]),
                "spectralFlux": _round(smoothed["spectralFlux"]),
                "onset": _round(onset),
                "transient": _round(smoothed["transient"]),
                "centroid": _round(smoothed["centroid"]),
                "pitchHz": _round(smoothed["pitchHz"]),
                "pitchNormalized": _round(smoothed["pitchNormalized"]),
                "pitchConfidence": _round(smoothed["pitchConfidence"]),
            }
        )
    return result


def _spectrum(
    samples: list[float],
    window: tuple[float, ...],
    window_gain: float,
) -> list[float]:
    values = [complex(samples[index] * window[index], 0.0) for index in range(FFT_SIZE)]
    _fft(values)
    result: list[float] = []
    for value in values[: FFT_SIZE // 2]:
        magnitude = abs(value) / max(window_gain, 1e-12)
        db = 20.0 * math.log10(max(magnitude, 1e-8))
        result.append(_clamp01((db + 90.0) / 70.0))
    return result


def _fft(values: list[complex]) -> None:
    size = len(values)
    j = 0
    for index in range(1, size):
        bit = size >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if index < j:
            values[index], values[j] = values[j], values[index]
    length = 2
    while length <= size:
        root = cmath.exp(-2j * math.pi / length)
        for start in range(0, size, length):
            factor = 1 + 0j
            half = length // 2
            for offset in range(half):
                even = values[start + offset]
                odd = values[start + offset + half] * factor
                values[start + offset] = even + odd
                values[start + offset + half] = even - odd
                factor *= root
        length *= 2


def _estimate_spectral_pitch(spectrum: list[float], bin_hz: float) -> tuple[float, float]:
    first = max(1, math.ceil(80 / bin_hz))
    last = min(len(spectrum) - 2, math.floor(1000 / bin_hz))
    if last <= first:
        return 0.0, 0.0
    peak_index = max(range(first, last + 1), key=spectrum.__getitem__)
    peak = spectrum[peak_index]
    average = sum(spectrum[first : last + 1]) / (last - first + 1)
    confidence = _clamp01((peak - average) / max(0.18, peak) * 1.15)
    left = spectrum[peak_index - 1]
    right = spectrum[peak_index + 1]
    denominator = left - 2 * peak + right
    shift = 0.0 if abs(denominator) < 1e-9 else 0.5 * (left - right) / denominator
    shift = min(0.5, max(-0.5, shift))
    return (peak_index + shift) * bin_hz, confidence


def _normalize_pitch(hz: float, min_hz: float = 80.0, max_hz: float = 1000.0) -> float:
    if hz <= min_hz:
        return 0.0
    if hz >= max_hz:
        return 1.0
    return math.log2(hz / min_hz) / math.log2(max_hz / min_hz)


def _smooth_toward(previous: float, target: float, rise: float, fall: float) -> float:
    factor = rise if target > previous else fall
    return previous + (target - previous) * factor


def _attack_release(
    previous: float,
    target: float,
    delta_ms: float,
    attack_ms: float,
    release_ms: float,
) -> float:
    tau = max(1.0, attack_ms if target > previous else release_ms)
    amount = 1.0 - math.exp(-max(0.0, delta_ms) / tau)
    return previous + (target - previous) * amount


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _soft_limit(value: float, knee: float = 0.85) -> float:
    if value <= knee:
        return value
    position = (value - knee) / (1 - knee)
    return knee + (1 - knee) * (1 - math.exp(-2.2 * position)) / (1 - math.exp(-2.2))


def _round(value: float) -> float:
    return round(value, 6)
