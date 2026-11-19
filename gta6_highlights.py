#!/usr/bin/env python3
"""Find gameplay highlights in a local VOD and render review previews."""

import argparse
from array import array
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
from statistics import median
import subprocess
import sys


SAMPLE_RATE = 8000


@dataclass(frozen=True)
class Highlight:
    start: float
    end: float
    peak_at: float
    score_db: float
    reason: str


def video_duration(source):
    raw = subprocess.check_output([
        'ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
        'stream=duration:format=duration', '-of', 'json', str(source),
    ])
    probe = json.loads(raw)
    if not probe.get('streams'):
        raise ValueError('The recording has no video stream.')
    duration = probe['streams'][0].get('duration')
    if duration in (None, 'N/A'):
        duration = probe.get('format', {}).get('duration', 'nan')
    duration = float(duration)
    if not math.isfinite(duration) or duration < 1:
        raise ValueError('The video must have a known duration of at least one second.')
    return duration


def audio_levels(source):
    levels = []
    with subprocess.Popen([
        'ffmpeg', '-nostdin', '-v', 'error', '-i', str(source), '-map', '0:a:0',
        '-ac', '1', '-af', f'aresample={SAMPLE_RATE}:async=1:first_pts=0',
        '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1',
    ], stdout=subprocess.PIPE) as ffmpeg:
        while chunk := ffmpeg.stdout.read(SAMPLE_RATE * 2):
            samples = array('h', chunk)
            if sys.byteorder != 'little':
                samples.byteswap()
            power = sum(sample * sample for sample in samples) / len(samples)
            levels.append(10 * math.log10(max(power / 32768**2, 1e-7)))
        if ffmpeg.wait():
            raise ValueError('Could not decode the first audio track. Check that the VOD has audio.')
    return levels


def known_ranges(path, duration):
    if path is None:
        return []
    records = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(records, list):
        raise ValueError('Known clips must be a JSON array.')
    ranges = []
    for clip in records:
        try:
            start, end = float(clip['start']), float(clip['end'])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('Each known clip needs numeric start and end seconds.') from exc
        if not 0 <= start < end <= duration:
            raise ValueError('Known clip ranges must be inside this recording.')
        ranges.append((start, end))
    return ranges


def rank_highlights(levels, duration, length, limit, known):
    length = min(duration, length)
    candidates = []
    for index, level in enumerate(levels[:math.ceil(duration)]):
        peak = min(index + 0.5, duration)
        neighbors = levels[max(0, index - 20):index] + levels[index + 1:index + 21]
        if not neighbors or level < -38:
            continue
        rise = level - median(neighbors)
        if rise < 6:
            continue
        start = max(0, min(peak - length * 0.4, duration - length))
        end = start + length
        if any(a <= peak < b or min(end, b) - max(start, a) >= (b - a) * 0.5
               for a, b in known):
            continue
        candidates.append(Highlight(start, end, peak, round(rise, 2),
                                    f'Audio rose {rise:.1f} dB above its local baseline.'))

    selected = []
    for candidate in sorted(candidates, key=lambda item: (-item.score_db, item.peak_at)):
        if all(candidate.end <= item.start or candidate.start >= item.end for item in selected):
            selected.append(candidate)
            if len(selected) == limit:
                break
    return selected


def render_preview(source, highlight, target):
    # Pad the frame to keep the full gameplay HUD and any facecam visible.
    subprocess.run([
        'ffmpeg', '-nostdin', '-v', 'error', '-n', '-ss', str(highlight.start),
        '-i', str(source), '-t', str(highlight.end - highlight.start),
        '-map', '0:v:0', '-map', '0:a:0', '-vf',
        'scale=720:1280:force_original_aspect_ratio=decrease:force_divisible_by=2,'
        'setsar=1,pad=720:1280:(ow-iw)/2:(oh-ih)/2,format=yuv420p',
        '-c:v', 'libx264', '-preset', 'veryfast', '-threads', '2', '-c:a', 'aac',
        '-movflags', '+faststart', str(target),
    ], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recording', type=Path, help='Local recording provided by the creator')
    parser.add_argument('--out', required=True, type=Path, help='New directory for the review batch')
    parser.add_argument('--known-clips', type=Path, help='JSON timestamp ranges from this same VOD')
    parser.add_argument('--creator', help='Creator credit to record in the manifest')
    parser.add_argument('--source-url', help='Original VOD URL to record in the manifest')
    parser.add_argument('--length', type=int, default=25, help='Preview duration in seconds')
    parser.add_argument('--limit', type=int, default=3, help='Maximum number of previews')
    args = parser.parse_args()
    if not args.recording.is_file():
        parser.error('recording must be an existing local file')
    if args.length < 1 or args.limit < 1:
        parser.error('--length and --limit must be positive')
    if args.out.exists():
        parser.error('--out must be a new directory')

    try:
        source = args.recording.resolve()
        duration = video_duration(source)
        known = known_ranges(args.known_clips, duration)
        highlights = rank_highlights(audio_levels(source), duration, args.length, args.limit, known)
        args.out.mkdir(parents=True)
        records = []
        for rank, highlight in enumerate(highlights, 1):
            name = f'highlight-{rank:02d}.mp4'
            render_preview(source, highlight, args.out / name)
            records.append({'rank': rank, 'preview': name, **asdict(highlight)})
        manifest = {'recording': source.name, 'source_url': args.source_url,
                    'creator': args.creator, 'duration': duration,
                    'known_ranges_checked': len(known), 'highlights': records}
        (args.out / 'highlights.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Error: {exc}\n')
    print(f'Wrote {len(records)} preview(s) and a manifest to {args.out}')


if __name__ == '__main__':
    main()
