# YouTube Live Stream Clipper

Automatically finds exciting moments in live YouTube and Twitch streams and turns them into vertical clips. It watches for chat activity and audio loudness spikes, then creates a short video with speech captions and a title inspired by chat.

The clipper runs locally while the stream is live. It does not upload clips anywhere.

## Features

- Records supported live streams with `yt-dlp` and `ffmpeg`.
- Detects highlights using chat volume and audio loudness together, or loudness alone.
- Creates vertical 9:16 clips in split, crop, or blurred-background layouts.
- Transcribes speech with `faster-whisper` and burns captions into clips.
- Generates a short title from chat and writes clip metadata to a JSON sidecar.
- Supports YouTube live chat and anonymous Twitch chat.
- Lets you press Enter to create a test clip from the latest recorded footage.

## Requirements

- Python 3.10 or later.
- `ffmpeg` and `ffprobe` on your `PATH`. The FFmpeg build should include `libx264` and `libass`.
- A supported live YouTube or Twitch stream.
- For YouTube, `yt-dlp` may also require a supported JavaScript runtime. See the [yt-dlp EJS guide](https://github.com/yt-dlp/yt-dlp/wiki/EJS) if YouTube extraction fails.

The first transcription may download the selected Whisper model. The default model is `large-v3-turbo`; it can require a substantial download and may run slowly without a compatible GPU.

## Installation

### Windows (PowerShell)

Install Python and FFmpeg first, and make sure `ffmpeg` and `ffprobe` are available on `PATH`. Then, from the project directory:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### macOS / Linux

Install Python and FFmpeg using your operating system's package manager, then run:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Quick start

```bash
python clipper.py "https://www.youtube.com/watch?v=VIDEO_ID"
python clipper.py "https://www.twitch.tv/CHANNEL"
```

The program creates clips in `clips/` and temporary recording data in `.clipper_work/`. Press **Enter** while it is running to create a test clip from recent footage. Press **Ctrl+C** to stop.

To see all command-line options:

```bash
python clipper.py --help
```

## Common options

| Option | Default | Description |
|---|---:|---|
| `--out DIR` | `clips` | Directory for finished videos and metadata. |
| `--work DIR` | `.clipper_work` | Directory for temporary segments and logs. |
| `--no-chat` | off | Detect highlights from loudness only. |
| `--vertical split` | `split` | Output layout: `split`, `crop`, or `blur`. |
| `--cam-region REGION` | `auto` | Facecam location: `auto`, a corner name, or `x,y,w,h` fractions. |
| `--bottom full` | `full` | In split mode, show the full stream frame below or use `game` to crop around the facecam. |
| `--hide-cam off` | `off` | In split mode, leave the facecam visible below, blur it, or cover it with black. |
| `--preview-layout` | off | Preview the stream layout and save preview images, then exit. |
| `--window SECONDS` | `10` | Chat and loudness measurement window. |
| `--warmup WINDOWS` | `12` | Number of windows used to build the initial baseline. |
| `--baseline WINDOWS` | `36` | Rolling baseline length. |
| `--pre SECONDS` | `20` | Seconds before the detected moment to start the clip. |
| `--length SECONDS` | `30` | Requested clip duration. |
| `--cooldown SECONDS` | `30` | Minimum time between automatically triggered clips. |
| `--whisper-model MODEL` | `large-v3-turbo` | Whisper model used for speech transcription. |
| `--language CODE` | `hi` | Spoken language; use an empty value for automatic detection. |
| `--script roman` | `roman` | Render Hindi captions in Roman or Devanagari script. |
| `--no-captions` | off | Disable speech transcription and captions. |
| `--ytdlp-args ...` | none | Pass additional arguments to the stream recorder; place this option last. |
| `-v`, `--verbose` | off | Log detailed chat and loudness measurements. |

For example:

```bash
# Check how the vertical layout will look
python clipper.py "https://www.twitch.tv/CHANNEL" --preview-layout

# Use a smaller model and inspect detection measurements
python clipper.py "https://www.youtube.com/watch?v=VIDEO_ID" --whisper-model base -v

# Disable chat detection
python clipper.py "https://www.twitch.tv/CHANNEL" --no-chat

# Provide browser cookies to yt-dlp (the option and its arguments must be last)
python clipper.py "https://www.youtube.com/watch?v=VIDEO_ID" --ytdlp-args --cookies-from-browser firefox
```

## Vertical layout

The default `split` layout creates a 1080 × 1920 video. It places the facecam in the upper half and the stream frame in the lower half, with captions around the seam. The facecam is **not hidden in the lower half by default**; use `--hide-cam blur` or `--hide-cam black` to cover it there.

The facecam location is estimated automatically. To inspect the result:

```bash
python clipper.py "STREAM_URL" --preview-layout
```

Preview images are written to `.clipper_work/`. If automatic detection picks the wrong area, specify a corner or normalized rectangle. For example:

```bash
python clipper.py "STREAM_URL" --cam-region bottom-left
python clipper.py "STREAM_URL" --cam-region 0.02,0.60,0.25,0.38
```

The rectangle values are fractions of the frame: `x,y,width,height`. If there is no facecam, try `--vertical blur` or `--vertical crop`.

## How highlight detection works

The script measures chat message counts and audio loudness in time windows. After the warmup period, it compares each measurement with a rolling baseline. By default, a chat and loudness spike must both occur within one window of each other for an automatic clip to be queued. Use `--no-chat` for loudness-only detection. Adjust `--chat-z`, `--loud-z`, `--min-chat`, `--min-loud-db`, and `--tolerance` to tune detection.

Old recording segments are removed after `--keep` seconds (default: 180), so temporary disk usage is limited during normal operation. Set `--keep 0` to keep all recorded segments.

## Output

Each finished clip is saved as an MP4 named with its timestamp and title, for example:

```text
clips/20261124_104500_stream-highlight.mp4
clips/20261124_104500_stream-highlight.json
```

The JSON sidecar contains the title, clip start time, detection statistics, transcript, and a sample of the chat messages used to generate the title. If subtitle rendering is unavailable in FFmpeg, the script may save an `.ass` subtitle file alongside the video.

## Troubleshooting

- **`ffmpeg` or `ffprobe` not found:** Install FFmpeg and add its `bin` directory to `PATH`; open a new terminal afterward.
- **YouTube stream cannot be resolved:** Update `yt-dlp` and check its JavaScript runtime requirements. Some streams may need browser cookies supplied through `--ytdlp-args`.
- **No clips are detected:** Allow the warmup period to finish, run with `-v`, and consider lowering the detection thresholds.
- **Too many clips:** Raise `--chat-z`, `--loud-z`, `--min-chat`, or `--min-loud-db`.
- **Captions are slow:** Try `--whisper-model base` or `--whisper-model small`. Smaller models may be less accurate.
- **Facecam is misplaced:** Run `--preview-layout` and set `--cam-region` manually.
- **Chat and audio spikes do not line up:** Adjust `--chat-shift` or `--tolerance`.

## Project files

```text
clipper.py       Live recording, chat/audio analysis, and clip generation
requirements.txt Python package dependencies
README.md        Setup, usage, options, and troubleshooting
```

## Responsible use

Use this tool only with streams and content you have permission to record, edit, and publish. Follow the platform's terms, copyright rules, privacy expectations, and applicable laws. You are responsible for reviewing and obtaining permission for generated clips before sharing them.

## License

No license has been selected for this repository yet. Add a `LICENSE` file before inviting reuse or contributions; without a license, normal copyright restrictions apply.
