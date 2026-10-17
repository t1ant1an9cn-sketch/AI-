import wave

import numpy as np
import pytest

from voicedetect.audio.recording import WavRecorder, recording_seconds
from voicedetect.config import recording_path


def test_wav_is_playable_while_recording_and_after_close(tmp_path):
    path = tmp_path / "rec" / "s1.wav"
    recorder = WavRecorder(path)
    recorder.write(np.full(1600, 0.5, dtype=np.float32))
    recorder.write(np.array([2.0, -2.0, 0.0], dtype=np.float32))  # 超出范围的值截到满幅

    # 还没关闭就能读到完整长度：程序中途崩溃时，已录下的部分仍能播放
    with wave.open(str(path)) as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
        assert audio.getnframes() == 1603
        samples = np.frombuffer(audio.readframes(1603), dtype="<i2")
    assert samples[0] == round(0.5 * 32767)
    assert samples[-3:].tolist() == [32767, -32767, 0]
    assert recording_seconds(path) == pytest.approx(1603 / 16000)

    recorder.close()
    with wave.open(str(path)) as audio:
        assert audio.getnframes() == 1603
    assert recorder.frames == 1603


def test_recording_path_rejects_ids_that_could_leave_the_folder():
    assert recording_path("a1b2c3d4e5f6").name == "a1b2c3d4e5f6.wav"
    for bad in ("..", "..\\x", "../x", "a/b", "", "x" * 65):
        with pytest.raises(ValueError, match="场次 ID 无效"):
            recording_path(bad)
