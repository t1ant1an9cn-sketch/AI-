import hashlib
from re import escape
import threading

import httpx
import pytest

from voicedetect.asr.models import DownloadCancelled, ModelError, ModelFile, ModelSpec, ModelStore

CONTENT = {"a.bin": b"A" * 300_000, "b.txt": "词表".encode() * 10}


def spec():
    return ModelSpec(
        id="demo",
        name="测试模型",
        folder="demo-1",
        files=tuple(
            ModelFile(
                name,
                len(data),
                hashlib.sha256(data).hexdigest(),
                {"hf-mirror": f"https://mirror.test/{name}", "huggingface": f"https://hf.test/{name}"},
            )
            for name, data in CONTENT.items()
        ),
    )


def server(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def serve(request, *, corrupt=False):
    data = CONTENT[request.url.path.strip("/")]
    if corrupt:
        data = b"X" * len(data)
    if "range" in request.headers:
        start = int(request.headers["range"].split("=")[1].rstrip("-"))
        return httpx.Response(206, content=data[start:])
    return httpx.Response(200, content=data)


def test_download_verifies_files_and_reports_progress(tmp_path):
    seen = []
    store = ModelStore(tmp_path, spec())
    assert not store.installed()
    store.download(progress=lambda done, total: seen.append((done, total)), client=server(serve))
    assert store.installed()
    assert store.file("a.bin").read_bytes() == CONTENT["a.bin"]
    assert seen[-1] == (store.spec.size, store.spec.size)
    assert not list(store.path.glob("*.part"))


def test_falls_back_to_the_next_source(tmp_path):
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == "mirror.test":
            return httpx.Response(503)
        return serve(request)

    ModelStore(tmp_path, spec()).download(client=server(handler))
    assert hosts[:2] == ["mirror.test", "hf.test"]


def test_corrupted_download_is_discarded_and_retried_elsewhere(tmp_path):
    def handler(request):
        return serve(request, corrupt=request.url.host == "mirror.test")

    store = ModelStore(tmp_path, spec())
    store.download(client=server(handler))
    assert store.file("a.bin").read_bytes() == CONTENT["a.bin"]


def test_interrupted_download_resumes_with_range(tmp_path):
    store = ModelStore(tmp_path, spec())
    store.path.mkdir(parents=True)
    (store.path / "a.bin.part").write_bytes(CONTENT["a.bin"][:100_000])
    ranges = []

    def handler(request):
        ranges.append(request.headers.get("range"))
        return serve(request)

    store.download(client=server(handler))
    assert ranges[0] == "bytes=100000-"
    assert store.file("a.bin").read_bytes() == CONTENT["a.bin"]


def test_failure_message_lists_every_source(tmp_path):
    message = "a.bin 下载失败。hf-mirror.com 镜像：服务器返回 404；HuggingFace：服务器返回 404"
    with pytest.raises(ModelError, match=escape(message)):
        ModelStore(tmp_path, spec()).download(client=server(lambda request: httpx.Response(404)))


def test_download_can_be_cancelled(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadCancelled):
        ModelStore(tmp_path, spec()).download(client=server(serve), cancel=cancel)


def test_import_from_folder_checks_hashes(tmp_path):
    folder = tmp_path / "downloaded"
    folder.mkdir()
    for name, data in CONTENT.items():
        (folder / name).write_bytes(data)
    store = ModelStore(tmp_path / "models", spec())
    store.import_from(folder)
    assert store.installed()

    (folder / "b.txt").write_bytes(b"wrong")
    with pytest.raises(ModelError, match=escape("b.txt 与预期的模型文件不一致")):
        ModelStore(tmp_path / "other", spec()).import_from(folder)
    (folder / "b.txt").unlink()
    with pytest.raises(ModelError, match=escape("缺少：b.txt")):
        ModelStore(tmp_path / "other", spec()).import_from(folder)
