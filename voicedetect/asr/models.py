"""本地识别模型的下载、校验和导入。

SenseVoice（识别文字）与 Silero VAD（判断有没有人说话）共约 230 MB，首次使用时下载到数据目录。
每个文件都核对大小和 SHA-256；下载中断后再次下载会续传；也可以从已有文件夹导入。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import shutil
import threading

import httpx

SOURCE_ORDER = {
    "auto": ("hf-mirror", "huggingface", "github"),
    "hf-mirror": ("hf-mirror", "huggingface", "github"),
    "huggingface": ("huggingface", "hf-mirror", "github"),
}
SOURCE_NAMES = {"hf-mirror": "hf-mirror.com 镜像", "huggingface": "HuggingFace", "github": "GitHub"}

_SENSEVOICE_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
_VAD_REPO = "csukuangfj/vad"


def _hf(repo: str, name: str) -> dict[str, str]:
    return {
        "hf-mirror": f"https://hf-mirror.com/{repo}/resolve/main/{name}",
        "huggingface": f"https://huggingface.co/{repo}/resolve/main/{name}",
    }


@dataclass(frozen=True)
class ModelFile:
    name: str
    size: int
    sha256: str
    urls: dict[str, str]


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    folder: str
    files: tuple[ModelFile, ...] = field(default_factory=tuple)

    @property
    def size(self) -> int:
        return sum(item.size for item in self.files)


SENSEVOICE = ModelSpec(
    id="sensevoice",
    name="SenseVoice 本地识别模型（中英日韩粤）",
    folder="sensevoice-2024-07-17-int8",
    files=(
        ModelFile(
            "model.int8.onnx",
            239233841,
            "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
            _hf(_SENSEVOICE_REPO, "model.int8.onnx"),
        ),
        ModelFile(
            "tokens.txt",
            315894,
            "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
            _hf(_SENSEVOICE_REPO, "tokens.txt"),
        ),
        ModelFile(
            "silero_vad_v5.onnx",
            2313101,
            "6b99cbfd39246b6706f98ec13c7c50c6b299181f2474fa05cbc8046acc274396",
            {
                **_hf(_VAD_REPO, "silero_vad_v5.onnx"),
                "github": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad_v5.onnx",
            },
        ),
    ),
)


class ModelError(RuntimeError):
    pass


class DownloadCancelled(ModelError):
    pass


Progress = Callable[[int, int], None]  # (已完成字节, 总字节)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ModelStore:
    """一个模型在数据目录里的文件夹。"""

    def __init__(self, root: Path, spec: ModelSpec = SENSEVOICE) -> None:
        self.spec = spec
        self.path = root / spec.folder

    def file(self, name: str) -> Path:
        return self.path / name

    def installed(self) -> bool:
        """文件齐全且大小正确（完整校验在下载和导入时做过）。"""
        return all(self._complete(item) for item in self.spec.files)

    def _complete(self, item: ModelFile) -> bool:
        target = self.file(item.name)
        return target.is_file() and target.stat().st_size == item.size

    def download(
        self,
        *,
        source: str = "auto",
        progress: Progress | None = None,
        cancel: threading.Event | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        order = SOURCE_ORDER.get(source, SOURCE_ORDER["auto"])
        self.path.mkdir(parents=True, exist_ok=True)
        own_client = client is None
        client = client or httpx.Client(
            follow_redirects=True, timeout=httpx.Timeout(30.0, connect=10.0), headers={"User-Agent": "voicedetect"}
        )
        done = 0
        try:
            for item in self.spec.files:
                if self._complete(item):
                    done += item.size
                    _report(progress, done, self.spec.size)
                    continue
                self._download_file(client, item, order, done, progress, cancel)
                done += item.size
        finally:
            if own_client:
                client.close()

    def _download_file(
        self,
        client: httpx.Client,
        item: ModelFile,
        order: tuple[str, ...],
        done: int,
        progress: Progress | None,
        cancel: threading.Event | None,
    ) -> None:
        target = self.file(item.name)
        partial = target.with_name(target.name + ".part")
        errors = []
        for source in order:
            url = item.urls.get(source)
            if not url:
                continue
            try:
                self._fetch(client, url, partial, item, done, progress, cancel)
            except DownloadCancelled:
                raise
            except Exception as exc:
                errors.append(f"{SOURCE_NAMES[source]}：{_describe(exc)}")
                continue
            if sha256_of(partial) != item.sha256:
                partial.unlink(missing_ok=True)
                errors.append(f"{SOURCE_NAMES[source]}：文件校验不通过")
                continue
            partial.replace(target)
            return
        raise ModelError(f"{item.name} 下载失败。" + "；".join(errors))

    def _fetch(
        self,
        client: httpx.Client,
        url: str,
        partial: Path,
        item: ModelFile,
        done: int,
        progress: Progress | None,
        cancel: threading.Event | None,
    ) -> None:
        offset = partial.stat().st_size if partial.exists() else 0
        if offset >= item.size:
            partial.unlink()
            offset = 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            if offset and response.status_code != 206:
                offset = 0  # 服务器不支持续传，从头下载
            with partial.open("ab" if offset else "wb") as handle:
                for chunk in response.iter_bytes(1 << 16):
                    if cancel is not None and cancel.is_set():
                        raise DownloadCancelled("下载已取消")
                    handle.write(chunk)
                    offset += len(chunk)
                    _report(progress, done + offset, self.spec.size)
        if offset != item.size:
            raise ModelError(f"下载不完整（{offset}/{item.size} 字节）")

    def import_from(self, folder: Path) -> None:
        """从已下载好的文件夹复制模型文件，逐个校验。"""
        folder = Path(folder)
        missing = [item.name for item in self.spec.files if not (folder / item.name).is_file()]
        if missing:
            raise ModelError(f"文件夹里缺少：{'、'.join(missing)}")
        for item in self.spec.files:
            if sha256_of(folder / item.name) != item.sha256:
                raise ModelError(f"{item.name} 与预期的模型文件不一致")
        self.path.mkdir(parents=True, exist_ok=True)
        for item in self.spec.files:
            if (folder / item.name).resolve() != self.file(item.name).resolve():
                shutil.copyfile(folder / item.name, self.file(item.name))

    def remove(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _report(progress: Progress | None, done: int, total: int) -> None:
    if progress is not None:
        progress(done, total)


def _describe(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"服务器返回 {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "连接超时"
    if isinstance(exc, httpx.TransportError):
        return "网络连接失败"
    return str(exc) or type(exc).__name__
