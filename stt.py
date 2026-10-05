"""字幕工具：阿里云百炼语音识别 + Apple Music 风格歌词播放器。

用法：
    python stt.py                          交互菜单
    python stt.py tr     音频 [音频...]      1. 识别：在音频旁生成同名 .srt / .lrc
    python stt.py play   音频或文件夹         2. 播放（列表 = 该文件夹里有字幕的音频）
    python stt.py trplay 音频                3. 识别并播放（已有字幕则直接播放，--force 重新识别）
    python stt.py batch  文件夹 [--force]     4. 批量识别文件夹里的音频（默认跳过已有字幕的）

API Key 取自环境变量 DASHSCOPE_API_KEY，或本文件夹的 .env（DASHSCOPE_API_KEY=...，见 .env.example）。
识别原文和原始结果保存在本文件夹的 output 子文件夹里。
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # 输出重定向时默认是 GBK，统一成 UTF-8 防乱码

HERE = Path(__file__).resolve().parent
OUT = HERE / "output"
MODEL = "fun-asr"
MAX_LEN = 20          # 字幕每行大约多少字后在标点处换行
TASK_LIMIT = 100      # 录音文件识别一个任务最多 100 个文件
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".amr", ".wma", ".mp4", ".webm"}


# ---------- 通用 ----------

def load_key() -> str | None:
    key = os.environ.get("DASHSCOPE_API_KEY")
    if key:
        return key
    env = HERE / ".env"
    if not env.exists():
        return None
    vals = {}
    for line in env.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip("'\"")
    return vals.get("DASHSCOPE_API_KEY") or vals.get("stt") or None


def natural_key(p: Path):
    return [int(s) if s.isdigit() else s for s in re.split(r"(\d+)", p.name.lower())]


def audio_files(folder: Path) -> list[Path]:
    return sorted((f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS), key=natural_key)


def subtitle_of(audio: Path) -> Path | None:
    for ext in (".lrc", ".srt"):
        if audio.with_suffix(ext).exists():
            return audio.with_suffix(ext)
    return None


def expand(paths) -> list[Path]:
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files += audio_files(p)
        elif p.is_file():
            files.append(p)
        else:
            print(f"找不到：{p}")
    return [f.resolve() for f in files]


# ---------- 字幕 ----------

def lrc_time(ms: int) -> str:
    return f"{ms // 60000:02d}:{ms // 1000 % 60:02d}.{ms // 10 % 100:02d}"


def srt_time(ms: int) -> str:
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def split_sentence(s: dict) -> list[tuple[int, int, str, list]]:
    """长句按逐字时间戳在标点处切开，返回 [(开始毫秒, 结束毫秒, 文本, 该行的词列表)]。"""
    words = s.get("words") or []
    if not words or len(s["text"]) <= MAX_LEN * 1.5:
        return [(s["begin_time"], s["end_time"], s["text"], words)]
    parts, buf, start, ws = [], "", None, []
    for w in words:
        if start is None:
            start = w["begin_time"]
        buf += w.get("text", "") + w.get("punctuation", "")
        ws.append(w)
        if (len(buf) >= MAX_LEN and w.get("punctuation")) or len(buf) >= MAX_LEN * 2:
            parts.append((start, w["end_time"], buf.strip(), ws))
            buf, start, ws = "", None, []
    if buf.strip():
        parts.append((start, words[-1]["end_time"], buf.strip(), ws))
    return parts


def write_subtitles(data: dict, audio: Path):
    rows = [r for t in data["transcripts"] for s in t["sentences"] for r in split_sentence(s)]
    # 增强版 LRC：[行时间]<字时间>字…<行结束>，带逐字时间，播放器靠它逐字点亮
    lrc = []
    for b, e, txt, ws in rows:
        if ws:
            body = "".join(f"<{lrc_time(w['begin_time'])}>{w.get('text', '')}{w.get('punctuation', '')}" for w in ws)
            lrc.append(f"[{lrc_time(b)}]{body}<{lrc_time(e)}>")
        else:
            lrc.append(f"[{lrc_time(b)}]{txt}")
    srt = [f"{i}\n{srt_time(b)} --> {srt_time(e)}\n{txt}\n" for i, (b, e, txt, _) in enumerate(rows, 1)]
    # 带 BOM，各种播放器识别中文更稳
    audio.with_suffix(".lrc").write_text("\n".join(lrc) + "\n", encoding="utf-8-sig")
    audio.with_suffix(".srt").write_text("\n".join(srt), encoding="utf-8-sig")

    OUT.mkdir(exist_ok=True)
    text = "\n".join(s["text"] for t in data["transcripts"] for s in t["sentences"])
    (OUT / f"{audio.stem}.txt").write_text(text, encoding="utf-8")
    # 原始结果留一份，以后改字幕格式不用重新识别（不再扣额度）
    (OUT / f"{audio.stem}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return len(rows)


# ---------- 识别 ----------

def transcribe(files: list[Path]) -> list[Path]:
    """识别一批音频，返回成功生成字幕的文件。"""
    if not files:
        return []
    key = load_key()
    if not key:
        sys.exit("找不到 API Key：请设置环境变量 DASHSCOPE_API_KEY，或复制 .env.example 为 .env 并填入。")
    import dashscope
    from dashscope.audio.asr import Transcription
    from dashscope.utils.oss_utils import OssUtils
    dashscope.api_key = key

    done = []
    for start in range(0, len(files), TASK_LIMIT):
        chunk = files[start:start + TASK_LIMIT]
        t0 = time.time()
        with tempfile.TemporaryDirectory() as td:
            def upload(item):
                idx, path = item
                # 临时存储的对象名取自文件名，复制成纯 ASCII 名字避免中文/空格出问题
                safe = Path(td) / f"audio_{idx}{path.suffix.lower()}"
                shutil.copyfile(path, safe)
                return OssUtils.upload(model=MODEL, file_path=str(safe), api_key=key)[0]

            print(f"上传 {len(chunk)} 个文件……")
            with ThreadPoolExecutor(max_workers=4) as pool:
                urls = list(pool.map(upload, enumerate(chunk)))
        print(f"  上传完成 {time.time() - t0:.1f}s，识别中……")

        task = Transcription.async_call(model=MODEL, file_urls=urls,
                                        headers={"X-DashScope-OssResourceResolve": "enable"})
        if task.status_code != 200:
            print(f"提交失败：{task.code} {task.message}")
            continue
        resp = Transcription.wait(task=task.output.task_id)
        print(f"  识别结束，用时 {time.time() - t0:.1f}s")
        if resp.status_code != 200:
            print(f"识别失败：{resp.code} {resp.message}")
            continue

        for item in resp.output["results"]:
            # 服务端返回的是解析后的 http 地址，靠上传时的 audio_<序号> 对应回原文件
            m = re.search(r"/audio_(\d+)\.", item.get("file_url", ""))
            src = chunk[int(m.group(1))] if m else None
            if not src:
                print(f"  无法对应原文件：{item.get('file_url')}")
                continue
            if item.get("subtask_status") != "SUCCEEDED":
                print(f"  ✗ {src.name}：{item.get('code')} {item.get('message')}")
                continue
            with urllib.request.urlopen(item["transcription_url"]) as r:
                n = write_subtitles(json.load(r), src)
            print(f"  ✓ {src.name}（{n} 行字幕）")
            done.append(src)
    return done


# ---------- 播放 ----------

def play(target: Path):
    target = target.resolve()
    folder = target if target.is_dir() else target.parent
    if target.is_file() and not subtitle_of(target):
        sys.exit(f"{target.name} 还没有字幕，请用「识别并播放」。")
    tracks = [a for a in audio_files(folder) if subtitle_of(a)]
    if not tracks:
        sys.exit(f"{folder} 里没有带字幕的音频，先识别。")
    start = tracks.index(target) if target in tracks else 0

    data = {"start": start, "tracks": []}
    for a in tracks:
        sub = subtitle_of(a)
        data["tracks"].append({
            "title": a.stem,
            "audioUrl": a.as_uri(),
            "lyricsText": sub.read_text(encoding="utf-8-sig"),
            "kind": sub.suffix[1:].lower(),
        })
    page = (HERE / "player.html").read_text(encoding="utf-8")
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")  # 防止 </script> 截断页面
    page = page.replace("/*__DATA__*/null", blob, 1)

    out = Path(tempfile.gettempdir()) / "lyric_player" / f"{folder.name or 'root'}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(page, encoding="utf-8")
    webbrowser.open(out.as_uri())
    print(f"已在浏览器打开（列表 {len(tracks)} 首，从「{tracks[start].stem}」开始）")


# ---------- 四个功能 ----------

def cmd_tr(paths):
    files = expand(paths)
    if not files:
        sys.exit("没有可识别的音频文件。")
    transcribe(files)


def cmd_trplay(path, force=False):
    audio = Path(path).resolve()
    if not audio.is_file():
        sys.exit(f"找不到音频：{audio}")
    if force or not subtitle_of(audio):
        if not transcribe([audio]):
            sys.exit("识别失败，无法播放。")
    play(audio)


def cmd_batch(folder, force=False, confirm=False):
    folder = Path(folder)
    if not folder.is_dir():
        sys.exit(f"不是文件夹：{folder}")
    allf = audio_files(folder)
    todo = allf if force else [a for a in allf if not subtitle_of(a)]
    print(f"{folder}：共 {len(allf)} 个音频，待识别 {len(todo)} 个"
          + ("" if force else f"（跳过已有字幕的 {len(allf) - len(todo)} 个）"))
    if not todo:
        return
    size = sum(a.stat().st_size for a in todo) / 1024 / 1024
    print(f"  合计 {size:.0f} MB，会按音频时长消耗识别额度")
    if confirm and input("开始识别？(y/N) ").strip().lower() != "y":
        print("已取消。")
        return
    ok = transcribe(todo)
    print(f"完成：成功 {len(ok)} / {len(todo)}")


def ask_path(prompt: str) -> str:
    # 支持把文件拖进终端：去掉 PowerShell 的 & 和引号
    s = input(prompt).strip()
    s = re.sub(r"^&\s*", "", s).strip().strip("'\"")
    return s


def menu():
    while True:
        print("\n===== 字幕工具 =====\n"
              "  1. 识别（生成字幕）\n"
              "  2. 播放\n"
              "  3. 识别并播放\n"
              "  4. 批量识别文件夹\n"
              "  0. 退出")
        c = input("选择：").strip()
        if c in ("0", "q", ""):
            return
        prompts = {"1": "音频文件或文件夹（可直接拖进来）：", "2": "音频文件或文件夹：", "3": "音频文件：", "4": "文件夹："}
        if c not in prompts:
            print("请输入 0-4。")
            continue
        p = ask_path(prompts[c])
        if not p:
            continue
        try:
            if c == "1":
                cmd_tr([p])
            elif c == "2":
                play(Path(p))
            elif c == "3":
                cmd_trplay(p)
            else:
                cmd_batch(p, confirm=True)
        except SystemExit as e:  # 菜单里出错不退出程序
            if e.code:
                print(e.code)
        except Exception as e:
            print(f"出错：{e}")


def main():
    if len(sys.argv) == 1:
        return menu()
    ap = argparse.ArgumentParser(description="字幕工具：识别 + 歌词播放器")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tr", help="识别（生成字幕）")
    p.add_argument("paths", nargs="+")
    p = sub.add_parser("play", help="播放")
    p.add_argument("path")
    p = sub.add_parser("trplay", help="识别并播放")
    p.add_argument("path")
    p.add_argument("--force", action="store_true", help="已有字幕也重新识别")
    p = sub.add_parser("batch", help="批量识别文件夹")
    p.add_argument("folder")
    p.add_argument("--force", action="store_true", help="已有字幕也重新识别")
    a = ap.parse_args()
    if a.cmd == "tr":
        cmd_tr(a.paths)
    elif a.cmd == "play":
        play(Path(a.path))
    elif a.cmd == "trplay":
        cmd_trplay(a.path, a.force)
    else:
        cmd_batch(a.folder, a.force)


if __name__ == "__main__":
    main()
