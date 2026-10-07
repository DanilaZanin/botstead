#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
INPUT_DIR="$REPO_DIR/e2e/demo-out"
OUTPUT_DIR="$REPO_DIR/docs/assets/demos"
MAX_BYTES=$((3 * 1024 * 1024))

if ! command -v ffmpeg >/dev/null 2>&1; then
  printf 'ffmpeg is required to convert demo videos.\n' >&2
  exit 1
fi

shopt -s nullglob
videos=("$INPUT_DIR"/*.webm)
if ((${#videos[@]} == 0)); then
  printf 'No WebM demos found in %s. Run record-demos.mjs first.\n' "$INPUT_DIR" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

file_size() {
  stat -f '%z' "$1" 2>/dev/null || stat -c '%s' "$1"
}

convert() {
  local input="$1" output="$2" fps="$3" width="$4" palette="$5"
  ffmpeg -hide_banner -loglevel warning -y -i "$input" \
    -vf "trim=start=0.5,setpts=PTS-STARTPTS,fps=${fps},scale=${width}:-2:flags=lanczos,palettegen=stats_mode=diff" "$palette"
  ffmpeg -hide_banner -loglevel warning -y -i "$input" -i "$palette" \
    -filter_complex "[0:v]trim=start=0.5,setpts=PTS-STARTPTS,fps=${fps},scale=${width}:-2:flags=lanczos[frames];[frames][1:v]paletteuse=dither=sierra2_4a" \
    "$output"
}

for input in "${videos[@]}"; do
  filename="${input##*/}"
  name="${filename%.webm}"
  width=960
  if [[ "$name" == "mobile" ]]; then width=360; fi

  output="$OUTPUT_DIR/$name.gif"
  palette="$tmp_dir/$name-palette.png"
  convert "$input" "$output" 12 "$width" "$palette"
  bytes="$(file_size "$output")"
  if ((bytes > MAX_BYTES)); then
    retry_width=800
    if ((width < retry_width)); then retry_width="$width"; fi
    printf '%s is %s bytes. Retrying at 10 fps and %s px wide.\n' "$name.gif" "$bytes" "$retry_width"
    convert "$input" "$output" 10 "$retry_width" "$palette"
    bytes="$(file_size "$output")"
  fi

  if ((bytes > MAX_BYTES)); then
    printf 'Warning: %s is still larger than 3 MiB (%s bytes).\n' "$output" "$bytes" >&2
  else
    printf 'Created %s (%s bytes).\n' "$output" "$bytes"
  fi
done
