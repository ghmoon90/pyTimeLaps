#!/usr/bin/env python3
"""pyTimeLaps - build a single timelapse clip from many video files.

Inputs are listed in a text file (one path or glob pattern per line), settings
come from a JSON file. Video work is done by ffmpeg/ffprobe, which must be on PATH.
"""

import argparse
import copy
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

DEFAULT_SETTINGS = {
    "output": {
        "path": "timelapse.mp4",
        "format": "mp4",            # mp4 | mkv | mov | webm | gif
        "resolution": "1920x1080",  # "WxH", "Wx-1" / "-1xH" (keep aspect), or "original"
        "scale_mode": "fit",        # fit (letterbox) | fill (crop) | stretch
        "fps": 30,
        "codec": None,              # None = pick by format (libx264 / libvpx-vp9)
        "crf": 23,
        "preset": "medium",
        "pix_fmt": "yuv420p",
    },
    "timing": {
        "mode": "speed",            # speed | total_duration | per_clip
        "speed": 60,                # mode=speed: 60 -> 1 min of input becomes 1 s
        "total_duration": 60,       # mode=total_duration: whole output length in seconds
        "per_clip_duration": 2,     # mode=per_clip: each kept segment becomes N seconds
    },
    "filter": {
        "time_ranges": [],          # daily windows, e.g. [["06:00", "18:30"]]; empty = keep all
        "mode": "trim",             # trim: cut clips to the windows | start: keep clips starting inside
        "date_from": None,          # "YYYY-MM-DD" (inclusive)
        "date_to": None,            # "YYYY-MM-DD" (inclusive)
        "weekdays": None,           # e.g. ["mon", "tue", "wed", "thu", "fri"]
    },
    "timestamp": {
        "source": "auto",           # auto | filename | metadata | mtime
        # Groups in order Y m d H M [S], or named groups (?P<Y>..)(?P<m>..)...
        "filename_regex": r"(\d{4})[-_]?(\d{2})[-_]?(\d{2})[-_T ]?(\d{2})[-_:]?(\d{2})[-_:]?(\d{2})?",
    },
    "sort": "time",                 # time | name | input
    "extensions": [".mp4", ".mov", ".mkv", ".avi", ".m4v", ".mts", ".m2ts", ".ts", ".webm", ".wmv", ".3gp"],
    "keyframes_only": False,        # decode only keyframes: much faster for huge speedups, choppier
    "jobs": 2,                      # parallel ffmpeg encodes
    "keep_temp": False,
}

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
VERBOSE = False


class TimelapseError(Exception):
    pass


@dataclass
class Clip:
    path: str
    duration: float
    width: int
    height: int
    start: datetime | None = None
    order: int = 0
    segments: list = field(default_factory=list)  # [(offset_s, length_s)] within the clip


# ---------------------------------------------------------------- settings

def deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(path):
    if not path:
        return copy.deepcopy(DEFAULT_SETTINGS)
    try:
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise TimelapseError(f"cannot read settings {path}: {e}")
    unknown = set(user) - set(DEFAULT_SETTINGS)
    if unknown:
        print(f"warning: unknown settings keys ignored: {', '.join(sorted(unknown))}", file=sys.stderr)
    return deep_merge(DEFAULT_SETTINGS, user)


# ---------------------------------------------------------------- inputs

def read_input_list(list_path, extensions):
    """Expand each line of the list file into concrete video paths."""
    base = os.path.dirname(os.path.abspath(list_path))
    exts = {e.lower() for e in extensions}
    seen, files = set(), []
    try:
        with open(list_path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise TimelapseError(f"cannot read input list {list_path}: {e}")

    for lineno, raw in enumerate(lines, 1):
        line = raw.strip().strip('"').strip("'")
        if not line or line.startswith("#"):
            continue
        pattern = os.path.expanduser(line)
        if not os.path.isabs(pattern):
            pattern = os.path.join(base, pattern)
        if glob.has_magic(pattern):
            matches = sorted(p for p in glob.glob(pattern, recursive=True)
                             if os.path.isfile(p) and os.path.splitext(p)[1].lower() in exts)
        else:
            matches = [pattern] if os.path.isfile(pattern) else []
        if not matches:
            print(f"warning: line {lineno}: nothing matches '{line}'", file=sys.stderr)
        for p in matches:
            p = os.path.abspath(p)
            if p not in seen:
                seen.add(p)
                files.append(p)
    return files


def probe(path):
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "format=duration:format_tags=creation_time:stream=width,height:stream_tags=creation_time",
           "-of", "json", path]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise TimelapseError(res.stderr.strip() or "ffprobe failed")
    info = json.loads(res.stdout)
    streams = info.get("streams") or []
    if not streams:
        raise TimelapseError("no video stream")
    fmt = info.get("format", {})
    duration = float(fmt.get("duration") or 0)
    if duration <= 0:
        raise TimelapseError("unknown duration")
    ctime = (fmt.get("tags") or {}).get("creation_time") or (streams[0].get("tags") or {}).get("creation_time")
    return duration, int(streams[0]["width"]), int(streams[0]["height"]), ctime


def time_from_filename(path, regex):
    m = re.search(regex, os.path.basename(path))
    if not m:
        return None
    g = m.groupdict()
    if g:
        parts = [g.get(k) for k in ("Y", "m", "d", "H", "M", "S")]
    else:
        parts = list(m.groups()) + [None] * 6
    try:
        nums = [int(p) if p else 0 for p in parts[:6]]
        return datetime(*nums)
    except (ValueError, TypeError):
        return None


def time_from_metadata(ctime):
    if not ctime:
        return None
    try:
        dt = datetime.fromisoformat(ctime.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.year < 1980:  # cameras without a clock often write 1970/1904
        return None
    if dt.tzinfo is not None:  # metadata is UTC; filter in local wall-clock time
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def resolve_start(path, ctime, ts_cfg):
    src = ts_cfg["source"]
    getters = {
        "filename": lambda: time_from_filename(path, ts_cfg["filename_regex"]),
        "metadata": lambda: time_from_metadata(ctime),
        "mtime": lambda: datetime.fromtimestamp(os.path.getmtime(path)),
    }
    if src == "auto":
        for name in ("filename", "metadata", "mtime"):
            dt = getters[name]()
            if dt:
                return dt
        return None
    if src not in getters:
        raise TimelapseError(f"timestamp.source must be auto|filename|metadata|mtime, got '{src}'")
    return getters[src]()


def collect_clips(files, settings):
    def work(item):
        i, path = item
        try:
            duration, w, h, ctime = probe(path)
        except TimelapseError as e:
            print(f"warning: skipping {path}: {e}", file=sys.stderr)
            return None
        return Clip(path, duration, w, h, resolve_start(path, ctime, settings["timestamp"]), i)

    with ThreadPoolExecutor(max_workers=8) as ex:
        clips = [c for c in ex.map(work, enumerate(files)) if c]

    order = settings["sort"]
    if order == "time":
        clips.sort(key=lambda c: (c.start or datetime.max, c.path))
    elif order == "name":
        clips.sort(key=lambda c: os.path.basename(c.path))
    elif order != "input":
        raise TimelapseError(f"sort must be time|name|input, got '{order}'")
    return clips


# ---------------------------------------------------------------- filtering

def parse_hhmm(s):
    try:
        return time.fromisoformat(s)
    except (ValueError, TypeError):
        raise TimelapseError(f"bad time '{s}', expected HH:MM or HH:MM:SS")


def parse_filter(fcfg):
    ranges = []
    for r in fcfg.get("time_ranges") or []:
        if isinstance(r, str):  # also accept "06:00-18:00"
            r = r.split("-")
        if len(r) != 2:
            raise TimelapseError(f"bad time range {r}, expected [\"HH:MM\", \"HH:MM\"]")
        ranges.append((parse_hhmm(r[0]), parse_hhmm(r[1])))
    d_from = date.fromisoformat(fcfg["date_from"]) if fcfg.get("date_from") else None
    d_to = date.fromisoformat(fcfg["date_to"]) if fcfg.get("date_to") else None
    wd = None
    if fcfg.get("weekdays"):
        try:
            wd = {WEEKDAYS.index(d.lower()[:3]) for d in fcfg["weekdays"]}
        except ValueError:
            raise TimelapseError(f"bad weekdays {fcfg['weekdays']}, use mon..sun")
    mode = fcfg.get("mode", "trim")
    if mode not in ("trim", "start"):
        raise TimelapseError(f"filter.mode must be trim|start, got '{mode}'")
    return ranges, d_from, d_to, wd, mode


def day_allowed(d, d_from, d_to, wd):
    return (d_from is None or d >= d_from) and (d_to is None or d <= d_to) and (wd is None or d.weekday() in wd)


def windows_between(s, e, ranges, d_from, d_to, wd):
    """Absolute [a, b) datetime windows overlapping s..e, merged."""
    out = []
    day = s.date() - timedelta(days=1)  # a window starting yesterday may cross midnight
    while day <= e.date():
        if day_allowed(day, d_from, d_to, wd):
            for t0, t1 in ranges or [(time(0), time(0))]:  # no ranges -> whole day
                a = datetime.combine(day, t0)
                b = datetime.combine(day, t1)
                if b <= a:
                    b += timedelta(days=1)
                if b > s and a < e:
                    out.append((max(a, s), min(b, e)))
        day += timedelta(days=1)
    out.sort()
    merged = []
    for a, b in out:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def apply_filter(clips, fcfg, min_len=0.5):
    ranges, d_from, d_to, wd, mode = parse_filter(fcfg)
    active = bool(ranges or d_from or d_to or wd is not None)
    kept = []
    for c in clips:
        if not active:
            c.segments = [(0.0, c.duration)]
        elif c.start is None:
            print(f"warning: no timestamp for {c.path}; excluded by time filter", file=sys.stderr)
            continue
        elif mode == "start":
            hit = windows_between(c.start, c.start + timedelta(seconds=0.001), ranges, d_from, d_to, wd)
            c.segments = [(0.0, c.duration)] if hit else []
        else:
            end = c.start + timedelta(seconds=c.duration)
            c.segments = [((a - c.start).total_seconds(), (b - a).total_seconds())
                          for a, b in windows_between(c.start, end, ranges, d_from, d_to, wd)
                          if (b - a).total_seconds() >= min_len]
        if c.segments:
            kept.append(c)
    return kept


# ---------------------------------------------------------------- encoding

def resolve_resolution(res, clips):
    ref = clips[0]
    if res in (None, "", "original"):
        w, h = ref.width, ref.height
    else:
        m = re.fullmatch(r"\s*(-?\d+)\s*[x:]\s*(-?\d+)\s*", str(res))
        if not m:
            raise TimelapseError(f"bad resolution '{res}', expected WxH, Wx-1 or original")
        w, h = int(m.group(1)), int(m.group(2))
        if w <= 0 and h <= 0:
            raise TimelapseError("resolution needs at least one positive side")
        if w <= 0:
            w = round(h * ref.width / ref.height)
        elif h <= 0:
            h = round(w * ref.height / ref.width)
    return w - w % 2, h - h % 2  # yuv420p needs even dimensions


def scale_filter(w, h, mode):
    if mode == "fit":
        return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if mode == "fill":
        return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"
    if mode == "stretch":
        return f"scale={w}:{h},setsar=1"
    raise TimelapseError(f"scale_mode must be fit|fill|stretch, got '{mode}'")


def codec_args(out):
    fmt = out["format"].lower()
    if fmt == "gif":  # near-lossless intermediates; the palette pass happens at concat
        return ["-c:v", "libx264", "-crf", "12", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    codec = out.get("codec") or ("libvpx-vp9" if fmt == "webm" else "libx264")
    args = ["-c:v", codec]
    if out.get("crf") is not None:
        args += ["-crf", str(out["crf"])]
    if "vpx" in codec:
        args += ["-b:v", "0", "-row-mt", "1"]
    elif out.get("preset") and codec in ("libx264", "libx265"):
        args += ["-preset", out["preset"]]
    if out.get("pix_fmt"):
        args += ["-pix_fmt", out["pix_fmt"]]
    return args


def compute_speeds(clips, tcfg):
    """Return one speed factor per (clip, segment), in order."""
    segs = [(c, off, ln) for c in clips for off, ln in c.segments]
    mode = tcfg["mode"]
    if mode == "speed":
        sp = float(tcfg["speed"])
        speeds = [sp] * len(segs)
    elif mode == "total_duration":
        total = sum(ln for _, _, ln in segs)
        sp = total / float(tcfg["total_duration"])
        speeds = [sp] * len(segs)
    elif mode == "per_clip":
        target = float(tcfg["per_clip_duration"])
        speeds = [ln / target for _, _, ln in segs]
    else:
        raise TimelapseError(f"timing.mode must be speed|total_duration|per_clip, got '{mode}'")
    if any(s <= 0 for s in speeds):
        raise TimelapseError("timing values must be positive")
    return segs, speeds


def run(cmd):
    if VERBOSE:
        print("  $ " + " ".join(f"'{a}'" if " " in a else a for a in cmd), file=sys.stderr)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise TimelapseError(f"ffmpeg failed:\n{res.stderr.strip()[-2000:]}")


def encode_segment(job):
    path, offset, length, speed, fps, vf, cargs, keyonly, dst = job
    frames = max(1, round(length / speed * fps))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
    if keyonly:
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-ss", f"{offset:.3f}", "-t", f"{length:.3f}", "-i", path,
            "-an", "-sn", "-dn",
            "-vf", f"setpts=(PTS-STARTPTS)/{speed:.6f},fps={fps},{vf}",
            "-frames:v", str(frames), *cargs, dst]
    run(cmd)
    return dst


def concat(segment_files, out_path, out_cfg, workdir):
    list_path = os.path.join(workdir, "concat.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for p in segment_files:
            f.write("file '" + p.replace("'", r"'\''") + "'\n")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
           "-f", "concat", "-safe", "0", "-i", list_path]
    fmt = out_cfg["format"].lower()
    if fmt == "gif":
        cmd += ["-vf", "split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer",
                "-loop", "0"]
    else:
        cmd += ["-c", "copy"]
        if fmt in ("mp4", "mov"):
            cmd += ["-movflags", "+faststart"]
    run(cmd + [out_path])


# ---------------------------------------------------------------- main

def fmt_dur(sec):
    sec = int(round(sec))
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def build(args):
    settings = load_settings(args.config)
    out_cfg = settings["output"]
    if args.output:
        out_cfg["path"] = args.output
        ext = os.path.splitext(args.output)[1].lstrip(".").lower()
        if ext in ("mp4", "mkv", "mov", "webm", "gif"):
            out_cfg["format"] = ext
    if out_cfg["format"].lower() not in ("mp4", "mkv", "mov", "webm", "gif"):
        raise TimelapseError(f"output.format must be mp4|mkv|mov|webm|gif, got '{out_cfg['format']}'")
    fps = float(out_cfg["fps"])

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise TimelapseError(f"{tool} not found on PATH")

    files = read_input_list(args.inputs, settings["extensions"])
    print(f"found {len(files)} file(s)")
    if not files:
        raise TimelapseError("no input videos")

    clips = collect_clips(files, settings)
    clips = apply_filter(clips, settings["filter"])
    if not clips:
        raise TimelapseError("no footage left after filtering")

    segs, speeds = compute_speeds(clips, settings["timing"])
    w, h = resolve_resolution(out_cfg["resolution"], clips)
    in_total = sum(ln for _, _, ln in segs)
    out_total = sum(max(1, round(ln / sp * fps)) for (_, _, ln), sp in zip(segs, speeds)) / fps

    print(f"{len(clips)} clip(s), {len(segs)} segment(s), input {fmt_dur(in_total)}"
          f" -> output ~{fmt_dur(out_total)} ({out_total:.1f}s) at {w}x{h} {fps:g}fps")
    if args.dry_run or VERBOSE:
        for (c, off, ln), sp in zip(segs, speeds):
            st = (c.start + timedelta(seconds=off)).strftime("%Y-%m-%d %H:%M:%S") if c.start else "?"
            print(f"  {st}  {os.path.basename(c.path)}  +{off:.1f}s  len {ln:.1f}s  x{sp:.1f}"
                  f"  -> {max(1, round(ln / sp * fps))} frame(s)")
    if args.dry_run:
        return 0

    vf = scale_filter(w, h, out_cfg["scale_mode"])
    cargs = codec_args(out_cfg)
    workdir = tempfile.mkdtemp(prefix="timelapse_")
    try:
        jobs = [(c.path, off, ln, sp, out_cfg["fps"], vf, cargs, settings["keyframes_only"],
                 os.path.join(workdir, f"seg_{i:05d}.mkv"))
                for i, ((c, off, ln), sp) in enumerate(zip(segs, speeds))]
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, int(settings["jobs"]))) as ex:
            futures = [ex.submit(encode_segment, j) for j in jobs]
            for fut in futures:
                fut.result()
                done += 1
                print(f"\rencoding segments {done}/{len(jobs)}", end="", flush=True)
        print()
        out_path = os.path.abspath(out_cfg["path"])
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        print("joining ...")
        concat([j[-1] for j in jobs], out_path, out_cfg, workdir)
        print(f"done: {out_path}")
    finally:
        if settings["keep_temp"]:
            print(f"temp files kept in {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    return 0


def main(argv=None):
    global VERBOSE
    p = argparse.ArgumentParser(description="Merge many videos into one timelapse clip.")
    p.add_argument("inputs", nargs="?", help="text file with one video path or glob pattern per line")
    p.add_argument("-c", "--config", help="JSON settings file (missing keys use defaults)")
    p.add_argument("-o", "--output", help="output file (overrides output.path; extension sets format)")
    p.add_argument("-n", "--dry-run", action="store_true", help="show the plan without encoding")
    p.add_argument("-v", "--verbose", action="store_true", help="print segments and ffmpeg commands")
    p.add_argument("--print-config", action="store_true", help="print the default settings JSON and exit")
    args = p.parse_args(argv)

    if args.print_config:
        print(json.dumps(DEFAULT_SETTINGS, indent=2))
        return 0
    if not args.inputs:
        p.error("the inputs list file is required")
    VERBOSE = args.verbose
    try:
        return build(args)
    except TimelapseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
