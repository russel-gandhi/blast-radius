#!/bin/sh
# Build and run the image on the sample set under the scorer's limits (make sample without make).
set -e
cd "$(dirname "$0")"
docker build -q --platform linux/amd64 -t br-entry . > /dev/null
W=$(pwd -W 2>/dev/null || pwd)
MSYS_NO_PATHCONV=1 docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 4096m --memory-swap 4096m \
  --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges \
  --user 65534:65534 --env HOME=/tmp -v "$W/${1:-sample}/commits:/data/commits:ro" -v "$W/${1:-sample}/tests.json:/data/tests.json:ro" \
  br-entry > output.jsonl 2> run.err
tail -1 run.err
python score.py ${1:-sample} ${1:-sample}/answers.json output.jsonl | head -3
