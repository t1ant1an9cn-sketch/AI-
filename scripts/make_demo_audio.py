"""生成演示音频：用 Windows 自带的中文语音朗读一段保温杯带货话术（只能在 Windows 上运行）。

输出 voicedetect/assets/demo/thermos-demo.ogg（Opus，16 kHz 单声道）和同名 .txt 话术文本。
话术故意包含违规表达、站外导流、一次 12 秒的冷场、一段偏快和一段偏慢的讲解，并且没讲赠品和售后，
用来体验和测试各类提醒。用法：uv run python scripts/make_demo_audio.py
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import wave

import av
import numpy as np

RATE = 16000
GAP_SECONDS = 0.7  # 句与句之间的自然停顿
VOICE = "Microsoft Huihui Desktop"
OUTPUT = Path(__file__).resolve().parents[1] / "voicedetect" / "assets" / "demo" / "thermos-demo.ogg"

# (语速, 文本)；语速为 SAPI 的 -10..10，"pause" 表示插入静音秒数。
SCRIPT: list[tuple[object, object]] = [
    (0, "哈喽哈喽，欢迎刚进直播间的宝宝们，新来的宝宝先点个关注，主播每天晚上八点准时开播。"),
    (0, "今天给大家带来的是我们家卖了十万单的爆款保温杯。嗯，就是说，这个杯子是我自己每天都在用的。"),
    (0, "先说材质，内胆用的是316不锈钢，食品级的材料，装热水、泡茶、装牛奶都放心。"),
    (0, "保温效果也特别好，早上八点灌的开水，到晚上八点，十二个小时以后打开还是烫的。"),
    (0, "容量是500毫升，一天喝水的量刚刚好，放在包里也不占地方。"),
    (
        7,
        "然后我们这个杯盖是双层密封设计的倒过来使劲摇都不会漏水开车上班出门旅游放在包里都特别方便"
        "颜色一共有三个有珍珠白曜石黑和雾霾蓝男生女生用都很好看买来送朋友送家人也很有面子",
    ),
    (0, "价格方面，今天直播间到手价只要99元，真的是全网最低价，错过今天就没有这个价格了。"),
    (0, "喜欢的宝宝直接点击下方小黄车，拍一号链接就可以了。"),
    ("pause", 12.0),
    (0, "嗯，好，我们继续。刚刚有宝宝在问能不能再便宜一点，想要更多优惠的可以加我微信私聊。"),
    (0, "我们这一批库存只剩最后两百单了，手慢无，还没下单的宝宝抓紧时间。"),
    (-5, "我再给大家，仔细地，演示一下。这个杯子，拿在手里，很轻。杯身，是磨砂的，不打滑。"),
    (0, "说实话，这个杯子真的是我用过最好的保温杯，没有之一。"),
    (0, "好啦，还没关注的宝宝点个关注，要下单的宝宝记得去小黄车一号链接，我们马上开始下一轮讲解。"),
    (0, "欢迎新进来的宝宝。再说一遍，今天这款保温杯，内胆是316不锈钢，容量500毫升，保温十二个小时。"),
    (0, "到手价99元，点击小黄车一号链接下单。"),
]

# 只含 ASCII，避免 PowerShell 按系统代码页解析脚本；中文通过 UTF-8 的 JSON 文件传入。
SYNTHESIZE = r"""
param([string]$Manifest)
Add-Type -AssemblyName System.Speech
$job = [System.IO.File]::ReadAllText($Manifest, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
$bits = [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen
$mono = [System.Speech.AudioFormat.AudioChannel]::Mono
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, $bits, $mono)
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SelectVoice($job.voice)
foreach ($line in $job.lines) {
  $synth.Rate = [int]$line.rate
  $synth.SetOutputToWaveFile($line.out, $format)
  $synth.Speak([string]$line.text)
}
$synth.SetOutputToNull()
$synth.Dispose()
"""


def synthesize(workdir: Path) -> list[np.ndarray | float]:
    lines = []
    for index, (rate, text) in enumerate(SCRIPT):
        if rate != "pause":
            lines.append({"rate": rate, "text": text, "out": str(workdir / f"{index:02d}.wav")})
    manifest = workdir / "job.json"
    manifest.write_text(json.dumps({"voice": VOICE, "lines": lines}, ensure_ascii=False), encoding="utf-8")
    script = workdir / "synthesize.ps1"
    script.write_text(SYNTHESIZE, encoding="ascii")
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), str(manifest)],
        check=True,
    )
    parts: list[np.ndarray | float] = []
    for index, (rate, value) in enumerate(SCRIPT):
        if rate == "pause":
            parts.append(float(value))
            continue
        with wave.open(str(workdir / f"{index:02d}.wav")) as audio:
            parts.append(np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2"))
    return parts


def assemble(parts: list[np.ndarray | float]) -> tuple[np.ndarray, list[str]]:
    chunks, notes, position = [np.zeros(int(RATE * 0.5), dtype="<i2")], [], 0.5
    texts = iter(text for rate, text in SCRIPT if rate != "pause")
    for part in parts:
        if isinstance(part, float):
            chunks.append(np.zeros(int(RATE * part), dtype="<i2"))
            notes.append(f"[{position:6.1f}s] （停顿 {part:.0f} 秒）")
            position += part
            continue
        notes.append(f"[{position:6.1f}s] {next(texts)}")
        chunks += [part, np.zeros(int(RATE * GAP_SECONDS), dtype="<i2")]
        position += len(part) / RATE + GAP_SECONDS
    return np.concatenate(chunks), notes


def encode(samples: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), "w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=RATE, layout="mono")
        stream.bit_rate = 32000
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = RATE
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        samples, notes = assemble(synthesize(Path(folder)))
    encode(samples, OUTPUT)
    OUTPUT.with_suffix(".txt").write_text("\n".join(notes) + "\n", encoding="utf-8")
    print(f"{OUTPUT}  {len(samples) / RATE:.1f} 秒")


if __name__ == "__main__":
    main()
