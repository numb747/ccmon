#!/usr/bin/env python3
"""给宣传片 ccmon-demo.mp4 配音：一层压低的背景音乐 + 操作音效。

    python3 scripts/demo-audio.py [输入.mp4] [输出.mp4] [音乐文件 [从第几秒开始]]

不给音乐文件就现场合成一段（很朴素）；给了就截取它垫在下面，音效照旧是合成的。
需要 numpy、scipy 和 ffmpeg。原片里页面自己响的提示音会保留。
音效时间点是对着画面逐帧量出来的，重新录了片子要改下面的 EVENTS。
"""
import subprocess, sys, tempfile, os
import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, sosfilt, fftconvolve

SR = 44100
SRC = sys.argv[1] if len(sys.argv) > 1 else "ccmon-demo.mp4"
DST = sys.argv[2] if len(sys.argv) > 2 else "ccmon-demo-music.mp4"
MUSIC = sys.argv[3] if len(sys.argv) > 3 else None
MUSIC_START = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0

# (秒, 类型)
EVENTS = [
    (0.35, "intro"),                      # logo 出现
    (5.35, "whoosh"),                     # 片头 → 看板
    (21.20, "wake"),                      # 睡着的会话醒来开工
    (23.77, "click"),                     # 点进会话详情
    (32.17, "tick"), (33.60, "tick"),     # 时间线滚出新的工具调用
    (38.43, "click"),                     # 切到另一个会话
    (42.40, "click"),                     # 点开小帮手
    (45.57, "click"),                     # 切到等授权的会话
    (48.73, "click"),                     # 回到总览
    (49.87, "switch"), (52.97, "switch"), # 浅色 / 深色皮肤
    (54.77, "switch"), (58.93, "switch"), # 像素农场 / 夜景公寓
    (63.37, "click"),                     # 回到默认主题
    (63.85, "whoosh"),                    # 缩进平板
    (66.73, "click"),                     # 平板上点一下
    (70.45, "whoosh"),                    # → 片尾
    (71.00, "outro"),
]

MUSIC_DB, SFX_DB, ORIG_DB = -21.0, -7.0, -2.0   # 音乐压低，音效在它上面一点
BPM = 72
rng = np.random.default_rng(7)


def hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def lowpass(x, cut, order=2):
    return sosfilt(butter(order, cut, fs=SR, output="sos"), x)


def bandpass(x, lo, hi):
    return sosfilt(butter(2, [lo, hi], btype="band", fs=SR, output="sos"), x)


def put(buf, t, snd, gain=1.0):
    i = int(t * SR)
    if i >= len(buf):
        return
    n = min(len(snd), len(buf) - i)
    buf[i:i + n] += snd[:n] * gain


def bell(midi, dur=1.6, bright=0.25):
    """电钢琴/音乐盒一样的软音头。"""
    t = np.arange(int(dur * SR)) / SR
    f = hz(midi)
    s = np.sin(2 * np.pi * f * t) + bright * np.sin(2 * np.pi * 2 * f * t) * np.exp(-t * 6) \
        + 0.08 * np.sin(2 * np.pi * 3.01 * f * t) * np.exp(-t * 10)
    return s * np.exp(-t * 3.2) * np.minimum(t / 0.004, 1)


def pad(midis, dur):
    """慢起慢落的铺底和弦。"""
    t = np.arange(int(dur * SR)) / SR
    s = np.zeros_like(t)
    for m in midis:
        for det in (-0.06, 0.05):
            f = hz(m + det)
            s += np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28)) + 0.3 * np.sin(2 * np.pi * 2 * f * t)
    env = np.minimum(t / 1.4, 1) * np.minimum((dur - t) / 1.6, 1)
    return lowpass(s, 900) * env / len(midis)


def music(total):
    beat = 60 / BPM
    bar = beat * 4
    n = int(total * SR)
    dry = np.zeros(n + SR * 4)
    # Cmaj7 – Am7 – Fmaj7 – G6，每个和弦两小节
    chords = [(48, [60, 64, 67, 71]), (45, [57, 60, 64, 67]),
              (41, [57, 60, 64, 65]), (43, [59, 62, 64, 67])]
    # 八分音符位置上稀疏的分解和弦：(拍, 和弦内第几个音, 八度)
    pattern = [(0, 0, 0), (1.5, 2, 0), (2.5, 1, 0), (4, 3, 0), (5.5, 2, 0), (6.5, 1, 12), (7, 0, 12)]
    k = 0
    while k * 2 * bar < total:
        root, notes = chords[k % 4]
        t0 = k * 2 * bar
        put(dry, t0, pad(notes, 2 * bar + 1.5), 0.55)
        put(dry, t0, pad([root], 2 * bar + 1.0), 0.5)
        if t0 >= 4.5:            # 片头只留铺底，看板出来了琴声再进
            for b, idx, octv in pattern:
                put(dry, t0 + b * beat + rng.uniform(-0.008, 0.008),
                    bell(notes[idx] + 12 + octv), rng.uniform(0.16, 0.24))
        k += 1
    dry = dry[:n]
    # 一点混响，左右用不同的尾巴拉开声场
    out = []
    for _ in range(2):
        tl = np.arange(int(2.2 * SR)) / SR
        ir = rng.standard_normal(len(tl)) * np.exp(-tl * 2.6)
        wet = fftconvolve(dry, lowpass(ir, 3500), mode="full")[:n]
        out.append(dry + 0.5 * wet / (np.abs(wet).max() + 1e-9) * np.abs(dry).max())
    st = np.stack(out, axis=1)
    t = np.arange(n) / SR
    fade = np.minimum(t / 2.0, 1) * np.minimum((total - t) / 3.5, 1)
    return st * np.clip(fade, 0, 1)[:, None]


def track(path, start, total):
    """从现成的曲子里截一段，头尾淡入淡出。"""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(start), "-t", str(total), "-i", path,
                          "-ac", "2", "-ar", str(SR), "-f", "s16le", "-"], check=True, capture_output=True).stdout
    a = np.frombuffer(raw, np.int16).reshape(-1, 2) / 32768
    a = np.vstack([a, np.zeros((max(0, int(total * SR) - len(a)), 2))])
    t = np.arange(len(a)) / SR
    return a * np.clip(np.minimum(t / 1.5, (total - t) / 4.0), 0, 1)[:, None]


def sfx(kind):
    if kind == "click":          # 圆润的一声“嗒”
        t = np.arange(int(0.09 * SR)) / SR
        f = 1500 * np.exp(-t * 28) + 520
        s = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 55)
        s[:220] += bandpass(rng.standard_normal(220), 2000, 6000) * np.linspace(0.5, 0, 220)
        return s
    if kind == "tick":
        t = np.arange(int(0.05 * SR)) / SR
        return 0.45 * np.sin(2 * np.pi * 2300 * t) * np.exp(-t * 110)
    if kind == "switch":         # 换主题：一声嗒 + 往上的两个音
        s = np.zeros(int(0.9 * SR))
        put(s, 0, sfx("click"), 0.9)
        put(s, 0.02, bell(84, 0.6, 0.15), 0.45)
        put(s, 0.10, bell(91, 0.75, 0.15), 0.45)
        return s
    if kind == "wake":           # 会话醒来：轻轻往上滑一下
        t = np.arange(int(0.22 * SR)) / SR
        f = 520 + 700 * (t / t[-1]) ** 0.6
        return 0.6 * np.sin(2 * np.pi * np.cumsum(f) / SR) * np.sin(np.pi * t / t[-1]) ** 1.5
    if kind == "whoosh":
        t = np.arange(int(0.75 * SR)) / SR
        nz = rng.standard_normal(len(t))
        s = 0.6 * bandpass(nz, 300, 1400) + 0.4 * bandpass(nz, 900, 3200) * (t / t[-1])
        return 0.55 * s * np.sin(np.pi * t / t[-1]) ** 2
    if kind == "intro":
        s = np.zeros(int(3.0 * SR))
        for i, m in enumerate((72, 76, 79, 84)):
            put(s, i * 0.11, bell(m, 2.4, 0.2), 0.5)
        return s
    if kind == "outro":
        s = np.zeros(int(3.5 * SR))
        for i, m in enumerate((84, 79, 76, 72)):
            put(s, i * 0.13, bell(m, 3.0, 0.2), 0.5)
        return s
    raise ValueError(kind)


def to_db(x, db):
    return x * (10 ** (db / 20) / (np.sqrt((x ** 2).mean()) + 1e-12))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        orig_wav, mix_wav = os.path.join(tmp, "orig.wav"), os.path.join(tmp, "mix.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", SRC, "-vn", "-ac", "2", "-ar", str(SR), orig_wav],
                       check=True)
        _, orig = wavfile.read(orig_wav)
        orig = orig.astype(np.float64) / 32768
        n = len(orig)
        total = n / SR

        bed = track(MUSIC, MUSIC_START, total) if MUSIC else music(total)
        mix = to_db(bed[:n], MUSIC_DB)

        fx = np.zeros(n)
        peak = 10 ** (SFX_DB / 20)
        for t, kind in EVENTS:
            s = sfx(kind)
            put(fx, t, s, peak / np.abs(s).max())
        mix += fx[:, None]
        mix += orig * 10 ** (ORIG_DB / 20)

        mix = np.clip(mix, -0.98, 0.98)                # 兜底，防削波
        wavfile.write(mix_wav, SR, (mix * 32767).astype(np.int16))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", SRC, "-i", mix_wav, "-map", "0:v", "-map", "1:a",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", DST], check=True)
    print("写好了", DST)


if __name__ == "__main__":
    main()
