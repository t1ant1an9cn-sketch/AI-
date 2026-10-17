"""生成程序图标 packaging/icon.ico（Windows）和 packaging/icon.icns（macOS），和界面 frontend/index.html
里的图标同一个图案。

图案在 32×32 的坐标里：圆角蓝底，五根白色竖条像声音的波形。只用 numpy 画，超采样抗锯齿，
每个尺寸存成 PNG 放进 ICO（Windows Vista 起支持）和 ICNS（macOS 10.7 起支持）。
用法：uv run python scripts/make_icon.py
"""

from __future__ import annotations

from pathlib import Path
import struct
import zlib

import numpy as np

BLUE = np.array([0x2A, 0x78, 0xD6], dtype=np.float64)
BARS = [(9, 14, 18), (13, 10, 22), (17, 7, 25), (21, 11, 21), (25, 14, 18)]  # (x, 上端, 下端)
BAR_RADIUS = 1.3  # 线宽 2.6，圆头
CORNER = 8.0
SIZES = (16, 24, 32, 48, 64, 128, 256)
# ICNS 里每种尺寸有固定的类型代码（带 @2x 的是给高分屏用的同一尺寸）
ICNS_TYPES = {
    b"icp4": 16,
    b"icp5": 32,
    b"ic11": 32,
    b"icp6": 64,
    b"ic12": 64,
    b"ic07": 128,
    b"ic08": 256,
    b"ic13": 256,
    b"ic09": 512,
    b"ic14": 512,
    b"ic10": 1024,
}


def render(size: int) -> np.ndarray:
    """size×size 的 RGBA 图像（uint8）。小图 4 倍超采样，大图少一些，免得占太多内存。"""
    supersample = max(1, min(4, 2048 // size))
    n = size * supersample
    coords = (np.arange(n) + 0.5) * 32 / n
    x, y = np.meshgrid(coords, coords)
    # 圆角矩形：把点收进内缩 CORNER 的矩形，离它的距离不超过 CORNER 就在里面
    dx = np.maximum(np.abs(x - 16) - (16 - CORNER), 0)
    dy = np.maximum(np.abs(y - 16) - (16 - CORNER), 0)
    inside = dx**2 + dy**2 <= CORNER**2
    bar = np.zeros_like(inside)
    for bx, top, bottom in BARS:
        nearest = np.clip(y, top, bottom)
        bar |= (x - bx) ** 2 + (y - nearest) ** 2 <= BAR_RADIUS**2
    rgb = np.where(bar[..., None], 255.0, BLUE)
    alpha = inside.astype(np.float64)
    image = np.dstack([rgb * alpha[..., None], alpha * 255])  # 先乘上透明度再缩小，边缘颜色才不发灰
    image = image.reshape(size, supersample, size, supersample, 4).mean(axis=(1, 3))
    covered = image[..., 3:] / 255
    image[..., :3] = np.where(covered > 0, image[..., :3] / np.maximum(covered, 1e-9), 0)
    return np.clip(np.round(image), 0, 255).astype(np.uint8)


def png(image: np.ndarray) -> bytes:
    height, width, _ = image.shape
    rows = b"".join(b"\x00" + image[row].tobytes() for row in range(height))  # 每行前面是过滤方式 0

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8 位 RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")


def ico(images: list[bytes], sizes: tuple[int, ...]) -> bytes:
    offset = 6 + 16 * len(images)
    entries, data = b"", b""
    for size, image in zip(sizes, images, strict=True):
        side = 0 if size >= 256 else size  # 256 在目录里记作 0
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32, len(image), offset + len(data))
        data += image
    return struct.pack("<HHH", 0, 1, len(images)) + entries + data


def icns(images: dict[int, bytes]) -> bytes:
    entries = b""
    for kind, size in ICNS_TYPES.items():
        entries += kind + struct.pack(">I", 8 + len(images[size])) + images[size]  # 长度含 8 字节的头
    return b"icns" + struct.pack(">I", 8 + len(entries)) + entries


def main() -> None:
    folder = Path(__file__).resolve().parent.parent / "packaging"
    folder.mkdir(exist_ok=True)
    pngs = {size: png(render(size)) for size in {*SIZES, *ICNS_TYPES.values()}}
    outputs = {
        "icon.ico": ico([pngs[size] for size in SIZES], SIZES),
        "icon.icns": icns(pngs),
        "icon.png": pngs[512],  # 说明文档用
    }
    for name, data in outputs.items():
        (folder / name).write_bytes(data)
        print(f"已生成 {folder / name}（{len(data)} 字节）")


if __name__ == "__main__":
    main()
