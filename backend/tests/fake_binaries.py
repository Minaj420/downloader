"""Shared fake yt-dlp / ffmpeg binaries used by job tests.

The fake yt-dlp parses ``-o`` from its argument array, emits one machine
readable PROG line, writes a fake media file, and prints its path — exactly
the contract the real adapter relies on.
"""

from pathlib import Path


def _write_script(path: Path, body: str) -> str:
    path.write_text(body)
    path.chmod(0o755)
    return str(path)


def fake_ytdlp_ok(directory: Path, *, media: str = "fake media") -> str:
    body = f"""#!/bin/sh
out=""
next=0
for a in "$@"; do
  if [ "$next" = "1" ]; then out="$a"; next=0; fi
  [ "$a" = "-o" ] && next=1
done
out=$(printf '%s' "$out" | sed 's/%(ext)s/mp4/g')
printf 'PROG 3 10 NA 1000000 7\\n'
printf '%s' '{media}' > "$out"
printf '%s\\n' "$out"
"""
    return _write_script(directory / "yt-dlp", body)


def fake_ytdlp_slow(directory: Path) -> str:
    body = """#!/bin/sh
out=""
next=0
for a in "$@"; do
  if [ "$next" = "1" ]; then out="$a"; next=0; fi
  [ "$a" = "-o" ] && next=1
done
out=$(printf '%s' "$out" | sed 's/%(ext)s/mp4/g')
printf 'PROG 1 100 NA 500000 99\\n'
printf '%s\\n' "$out"
exec sleep 30
"""
    return _write_script(directory / "yt-dlp", body)


def fake_ytdlp_steps(directory: Path, *, media: str = "fake media") -> str:
    """Fast for the ``video`` step, slow for the ``audio`` step."""
    body = f"""#!/bin/sh
out=""
next=0
for a in "$@"; do
  if [ "$next" = "1" ]; then out="$a"; next=0; fi
  [ "$a" = "-o" ] && next=1
done
case "$out" in
  */video.*)
    out=$(printf '%s' "$out" | sed 's/%(ext)s/mp4/g')
    printf 'PROG 3 10 NA 1000000 7\\n'
    printf '%s' '{media}' > "$out"
    printf '%s\\n' "$out"
    ;;
  *)
    out=$(printf '%s' "$out" | sed 's/%(ext)s/webm/g')
    printf 'PROG 2 8 NA 500000 3\\n'
    printf '%s' '{media}' > "$out"
    printf '%s\\n' "$out"
    exec sleep 30
    ;;
esac
"""
    return _write_script(directory / "yt-dlp", body)


def fake_ytdlp_fail(directory: Path, stderr: str) -> str:
    escaped = stderr.replace("'", "'\\''")
    body = f"""#!/bin/sh
echo 'ERROR: {escaped}' >&2
exit 1
"""
    return _write_script(directory / "yt-dlp", body)


def fake_ffmpeg_ok(directory: Path) -> str:
    body = """#!/bin/sh
out=""
for a in "$@"; do out="$a"; done
printf 'merged-media' > "$out"
"""
    return _write_script(directory / "ffmpeg", body)


def fake_ffmpeg_slow(directory: Path, *, media: str = "merged-media") -> str:
    body = f"""#!/bin/sh
out=""
for a in "$@"; do out="$a"; done
printf '%s' '{media}' > "$out"
exec sleep 30
"""
    return _write_script(directory / "ffmpeg", body)


def fake_ffmpeg_fail(directory: Path) -> str:
    body = """#!/bin/sh
echo 'Error: cannot merge' >&2
exit 1
"""
    return _write_script(directory / "ffmpeg", body)
