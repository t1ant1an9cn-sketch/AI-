"""测试公共设置：每个测试的数据目录都是临时文件夹，识别模型沿用本机已下载的那一份。"""

import pytest

from voicedetect.asr.models import SENSEVOICE, ModelStore
from voicedetect.config import models_dir

REAL_MODELS = models_dir()  # 在改掉数据目录之前取，测试仍能用到已下载的模型
requires_model = pytest.mark.skipif(
    not ModelStore(REAL_MODELS, SENSEVOICE).installed(), reason="未下载识别模型（voicedetect models download）"
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICEDETECT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VOICEDETECT_MODELS", str(REAL_MODELS))
