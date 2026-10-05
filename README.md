# lyric-player

用阿里云百炼 `fun-asr` 把音频（课程、播客、讲座等）转成**带逐字时间戳的字幕**，再用一个 Apple Music 风格的网页播放器边听边看，当前句逐字点亮。

- 识别结果写成同名 `.lrc`（增强版，逐字时间）和 `.srt`，放在音频旁边，其他播放器也能用
- 播放器是单个 `player.html`，无需服务器、无第三方依赖
- 播放列表 = 文件夹里所有带字幕的音频，记住每首的播放进度和是否听完

## 安装

需要 Python 3.10+。

```bash
pip install -r requirements.txt
cp .env.example .env   # 填入 DASHSCOPE_API_KEY
```

也可以直接设置环境变量 `DASHSCOPE_API_KEY`。

## 用法

```bash
python stt.py                          # 交互菜单（支持把文件拖进终端）
python stt.py tr     音频 [音频...]      # 识别：在音频旁生成同名 .srt / .lrc
python stt.py play   音频或文件夹         # 在浏览器里播放
python stt.py trplay 音频 [--force]      # 识别并播放（已有字幕则直接播放）
python stt.py batch  文件夹 [--force]     # 批量识别，默认跳过已有字幕的
```

识别按音频时长计费。每次识别的纯文本和原始 JSON 会存到 `output/`（已加入 `.gitignore`），以后调整字幕格式不必重新识别。

不用 Python 也能播放：直接用浏览器打开 `player.html`，把音频和同名 `.lrc` / `.srt` 一起拖进去。

## 播放器快捷键

| 按键 | 作用 |
| --- | --- |
| 空格 | 播放 / 暂停 |
| ← / → | 后退 / 前进 5 秒 |
| P / N | 上一首 / 下一首 |
| L | 显示 / 隐藏播放列表 |

点击任意一行字幕跳转到该处；在歌词区滚动鼠标可自由浏览，2.5 秒后自动回到当前行。

## 文件

| 文件 | 说明 |
| --- | --- |
| `stt.py` | 上传、识别、生成字幕、打开播放器 |
| `player.html` | 播放器页面，`stt.py play` 把曲目数据注入后写到临时目录打开 |
