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

### Timing modes

- `"speed"` with `"speed": 60`: every 60 s of footage becomes 1 s of output.
- `"total_duration"` with `"total_duration": 90`: the speed is chosen so the whole output is about 90 s.
- `"per_clip"` with `"per_clip_duration": 2`: each kept clip or segment becomes 2 s, whatever its length.

Each kept segment always gives at least one frame. Audio is dropped.

Metadata `creation_time` is stored in UTC and converted to local time before the daily filter is applied;
filename timestamps are used as-is.
