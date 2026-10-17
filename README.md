# pyTimeLaps

CLI tool that merges many video files into one timelapse clip.
Pure Python standard library (3.10+); requires `ffmpeg` and `ffprobe` on PATH.

```bash
python timelapse.py inputs.txt -c settings.json            # build
python timelapse.py inputs.txt -c settings.json -n         # dry run: show what would be used
python timelapse.py inputs.txt -c settings.json -o day.gif # override output (extension sets format)
python timelapse.py --print-config > settings.json         # dump all defaults
```

Flags: `-c/--config` settings JSON, `-o/--output`, `-n/--dry-run`, `-v/--verbose` (segment list + ffmpeg commands).

## Input list (`inputs.txt`)

One path or glob per line; `#` comments and blank lines are ignored; relative paths are
resolved from the list file's folder. `*` / `?` match within a name, `**` matches sub-folders.
Glob matches are limited to the `extensions` setting; duplicates are removed.

```
/media/cam/2026-10-*/VID_*.mp4
footage/**/GOPR*.MP4
```

## Settings (`settings.json`)

Any key you omit uses the default (see `--print-config`).

| key | meaning |
|---|---|
| `output.path` / `format` | output file; `mp4`, `mkv`, `mov`, `webm`, `gif` |
| `output.resolution` | `"1920x1080"`, `"1280x-1"` (keep aspect of the first clip), or `"original"` |
| `output.scale_mode` | `fit` (letterbox), `fill` (crop), `stretch` |
| `output.fps`, `codec`, `crf`, `preset`, `pix_fmt` | encoding options (`codec: null` = libx264, or libvpx-vp9 for webm) |
| `timing.mode` | how input time maps to output time, see below |
| `filter.time_ranges` | daily windows, e.g. `[["06:00","09:00"], "17:00-19:30"]`; `["22:00","04:00"]` crosses midnight; empty = all |
| `filter.mode` | `trim`: cut clips to the windows exactly · `start`: keep a whole clip if it starts inside a window |
| `filter.date_from` / `date_to` | inclusive `YYYY-MM-DD` limits |
| `filter.weekdays` | e.g. `["mon","tue","wed","thu","fri"]` (a window that crosses midnight belongs to the day it starts) |
| `timestamp.source` | `auto` (filename → metadata → file mtime), `filename`, `metadata`, `mtime` |
| `timestamp.filename_regex` | groups Y m d H M [S] in order, or named `(?P<Y>..)`; default matches `20261015_173000`, `2026-10-15_17-30-00`, … |
| `sort` | `time`, `name`, or `input` (list-file order) |
| `keyframes_only` | decode keyframes only: much faster for very large speed-ups, but choppier |
| `jobs` | parallel ffmpeg encodes |
| `keep_temp` | keep the per-segment intermediate files |

Settings files are strict JSON: strings (including dates such as `"2026-10-12"`) need double quotes,
booleans are lowercase `true` / `false`, "no value" is `null`, and trailing commas are not allowed.

A complete example with both timing and keyframe options:

```json
{
  "output":  { "path": "out/week.mp4", "resolution": "1920x1080", "fps": 30 },
  "timing":  { "mode": "speed", "speed": 120 },
  "filter":  { "time_ranges": [["06:30", "20:00"]], "date_from": "2026-10-12", "date_to": "2026-10-16" },
  "keyframes_only": true,
  "jobs": 4
}
```

## Timing (`timing`)

The `timing` block decides how fast the footage plays. It works on the footage **left after
filtering**: if `filter.time_ranges` keeps 06:30–20:00, only those 13.5 hours per day are counted.

```json
"timing": {
  "mode": "speed",            // which of the three rules below to use
  "speed": 60,                // used only when mode = "speed"
  "total_duration": 60,       // used only when mode = "total_duration" (seconds)
  "per_clip_duration": 2      // used only when mode = "per_clip" (seconds)
}
```

(Comments are only for illustration; JSON does not allow them.) You only need `mode` plus the one
value that mode uses; the others are ignored. All values must be positive and may be decimals.

### `"mode": "speed"` — fixed speed-up factor

```json
"timing": { "mode": "speed", "speed": 120 }
```

Every `speed` seconds of footage become 1 second of output, so
**output length = kept footage ÷ speed**. To pick a value, use
`speed = footage seconds you want per output second`:

| speed | 1 s of output shows | 1 hour of footage becomes | 13.5 h day becomes |
|---:|---|---:|---:|
| 10 | 10 s | 6 min | 81 min |
| 60 | 1 min | 60 s | 13.5 min |
| 120 | 2 min | 30 s | 6.75 min |
| 300 | 5 min | 12 s | 2.7 min |
| 600 | 10 min | 6 s | 81 s |
| 3600 | 1 hour | 1 s | 13.5 s |

Use this when you want the same motion speed across runs, whatever the amount of footage.

### `"mode": "total_duration"` — fixed output length

```json
"timing": { "mode": "total_duration", "total_duration": 90 }
```

The tool adds up all kept footage and sets `speed = kept footage ÷ total_duration`, so the whole
output is about 90 s. Example: 5 days × 13.5 h = 243 000 s of footage → speed 2700. Use this when the
result has to fit a length (a 60 s social clip, a 3 min summary), whatever the input.

### `"mode": "per_clip"` — fixed length per clip/segment

```json
"timing": { "mode": "per_clip", "per_clip_duration": 2 }
```

Every kept segment gets exactly 2 s of output, whatever its length: a 10 min clip is played at
×300, a 1 min clip at ×30. Output length = number of segments × `per_clip_duration`.
Note that with `filter.mode: "trim"`, one file that spans two windows (e.g. 08:00–18:00 against
`[["06:00","09:00"],["17:00","19:30"]]`) becomes two segments, so it gets 2 × 2 s. Use this for
"one shot per event" footage such as motion-triggered cameras, where clip lengths vary widely.

### How frames are counted

Each segment becomes `round(segment length ÷ speed × output.fps)` frames, at least 1. So:

- `output.fps` changes how smooth the result is, **not** its length (length is set by `timing`).
- One output frame covers `speed ÷ fps` seconds of footage (×120 at 30 fps → one frame per 4 s).
- With many short segments the "at least one frame" rule and rounding can make the result a little
  longer than the formula says, so `total_duration` is approximate.

Check the numbers before encoding with a dry run; it prints the speed and frame count of every segment:

```bash
python timelapse.py inputs.txt -c settings.json -n
# 3 clip(s), 5 segment(s), input 40:30:00 -> output ~0:20:15 (1215.0s) at 1920x1080 30fps
#   2026-10-12 06:30:00  cam_20261012_060000.mp4  +1800.0s  len 9000.0s  x120.0  -> 2250 frame(s)
```

Audio is always dropped.

## Keyframes only (`keyframes_only`)

A **top-level** boolean (not inside `output` or `timing`), default `false`. There is no command-line
flag; set it in the settings file:

```json
{
  "timing": { "mode": "speed", "speed": 600 },
  "keyframes_only": true
}
```

Normally ffmpeg decodes every frame of the input and then throws most of them away to reach the
speed-up. With `keyframes_only: true` it passes `-skip_frame nokey` to the decoder, so only keyframes
(I-frames) are decoded and all frames in between are never decoded at all. For long footage that
is several times faster and uses much less CPU.

The cost: the only pictures available are the keyframes, spaced by the camera's keyframe interval
(GOP), typically 1–4 s for CCTV/IP cameras and ~1 s for phones and action cameras. It looks fine when
**one output frame covers at least one keyframe interval**, i.e. when `speed ÷ fps ≥ keyframe interval`:

| speed | fps | footage per output frame | 2 s keyframe interval |
|---:|---:|---:|---|
| 30 | 30 | 1 s | choppy: each keyframe is repeated ~2× |
| 120 | 30 | 4 s | fine |
| 600 | 30 | 20 s | fine, and much faster |

Below that point the same keyframe is repeated for several output frames and motion stutters; leave
it `false` there (or lower `output.fps`). You can see the keyframe interval of a file with:

```bash
ffprobe -v error -select_streams v:0 -skip_frame nokey -show_entries frame=pts_time \
        -of csv=p=0 -read_intervals %+30 VIDEO.mp4
```

The printed times are the keyframe positions in the first 30 s; their spacing is the interval.
If very short segments (shorter than the keyframe interval) fail or come out empty, turn the
option off.

Metadata `creation_time` is stored in UTC and converted to local time before the daily filter is applied;
filename timestamps are used as-is.
