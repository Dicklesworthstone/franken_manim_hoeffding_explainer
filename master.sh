#!/usr/bin/env bash
# Join the chapter MP4s in the current directory and master the audio:
# the video stream is copied bit-for-bit; the narration is loudness-normalized
# (two-pass EBU R128 loudnorm: -16 LUFS integrated, -1.5 dBTP) and re-encoded
# as 256 kb/s AAC.
#
#   cd renders/final_4k && ../../master.sh [output.mp4]
set -euo pipefail
OUT="${1:-hoeffdings_d_explainer_4k.mp4}"
printf "file '%s'\n" 0*.mp4 > concat.txt
TARGET="I=-16:TP=-1.5:LRA=11"

stats=$(ffmpeg -hide_banner -nostats -f concat -safe 0 -i concat.txt -vn \
    -af "loudnorm=${TARGET}:print_format=json" -f null - 2>&1 | sed -n '/^{/,/^}/p')
get() { printf '%s' "$stats" | sed -n "s/.*\"$1\" : \"\\([^\"]*\\)\".*/\\1/p"; }
measured="measured_I=$(get input_i):measured_TP=$(get input_tp):measured_LRA=$(get input_lra):measured_thresh=$(get input_thresh):offset=$(get target_offset)"
echo "pass 1: $measured"

ffmpeg -hide_banner -loglevel error -y -f concat -safe 0 -i concat.txt \
    -map 0:v:0 -map 0:a:0 -c:v copy \
    -af "loudnorm=${TARGET}:${measured}:linear=true,aresample=48000" \
    -c:a aac -b:a 256k -movflags +faststart "$OUT"
echo "mastered -> $OUT"
