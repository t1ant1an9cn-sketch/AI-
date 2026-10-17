import time
import wave

import av
import numpy as np
import pytest

from voicedetect.asr.base import SAMPLE_RATE
from voicedetect.audio.base import level_db
from voicedetect.audio.files import MediaSource, is_stream_url


def write_wav(path, seconds, *, rate=44100, channels=2, frequency=440.0):
    t = np.arange(int(seconds * rate)) / rate
    tone = (0.3 * np.sin(2 * np.pi * frequency * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(np.repeat(tone[:, None], channels, axis=1).tobytes())
    return path


def write_flac(path, seconds, *, rate=48000, frequency=1000.0):
    with av.open(str(path), "w") as container:
        stream = container.add_stream("flac", rate=rate, layout="mono")
        t = np.arange(int(seconds * rate)) / rate
        samples = (0.3 * np.sin(2 * np.pi * frequency * t) * 32767).astype(np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
        frame.sample_rate = rate
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return path


def read_all(source):
    source.open()
    blocks = []
    try:
        while (block := source.read()) is not None:
            blocks.append(block)
    finally:
        source.close()
    return blocks


def dominant_frequency(samples):
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    return np.fft.rfftfreq(len(samples), 1 / SAMPLE_RATE)[np.argmax(spectrum)]


def test_wav_is_resampled_to_16k_mono_blocks(tmp_path):
    blocks = read_all(MediaSource(write_wav(tmp_path / "tone.wav", 2.0)))
    audio = np.concatenate(blocks)
    assert {len(b) for b in blocks[:-1]} == {1600}
    assert audio.dtype == np.float32
    assert len(audio) == pytest.approx(2.0 * SAMPLE_RATE, abs=200)
    assert dominant_frequency(audio[4000:20000]) == pytest.approx(440, abs=3)
    assert level_db(audio) == pytest.approx(20 * np.log10(0.3 / np.sqrt(2)), abs=0.5)


def test_compressed_formats_decode_too(tmp_path):
    source = MediaSource(write_flac(tmp_path / "tone.flac", 1.5))
    audio = np.concatenate(read_all(source))
    assert source.duration == pytest.approx(1.5, abs=0.05)
    assert len(audio) == pytest.approx(1.5 * SAMPLE_RATE, abs=200)
    assert dominant_frequency(audio[2000:18000]) == pytest.approx(1000, abs=3)


def test_realtime_playback_waits_for_the_clock(tmp_path):
    source = MediaSource(write_wav(tmp_path / "short.wav", 0.6, rate=16000, channels=1), realtime=True)
    started = time.monotonic()
    read_all(source)
    assert time.monotonic() - started >= 0.5


def test_missing_and_invalid_files_have_clear_errors(tmp_path):
    with pytest.raises(FileNotFoundError, match="找不到文件"):
        MediaSource(tmp_path / "missing.wav").open()
    bad = tmp_path / "notes.txt"
    bad.write_text("这不是音频", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"无法打开文件|没有音轨"):
        MediaSource(bad).open()


def test_stream_urls_are_live_sources():
    assert is_stream_url("https://example.com/live.flv")
    assert not is_stream_url("D:/录播/第一场.mp4")
    source = MediaSource("rtmp://example.com/live/room", realtime=True)
    assert source.live and not source.realtime and source.label.startswith("直播流")
