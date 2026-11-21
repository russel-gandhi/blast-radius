#!/bin/sh
# usage: tune.sh "ENV=.. ENV=.." -> total (simulated + cpu) over sample and mut1
W=$(pwd -W)
tot=0
for set in sample mut1; do
  MSYS_NO_PATHCONV=1 docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --user 65534:65534 --env HOME=/tmp $(for e in $1; do printf -- "-e %s " $e; done) -v "$W/$set/commits:/data/commits:ro" -v "$W/$set/tests.json:/data/tests.json:ro" br-entry > /tmp/o_$set.jsonl 2> /tmp/e_$set.log
  sim=$(python score.py $set $set/answers.json /tmp/o_$set.jsonl | sed -n 's/Simulated: *\([0-9.]*\).*/\1/p')
  miss=$(python score.py $set $set/answers.json /tmp/o_$set.jsonl | sed -n 's/Misses: *\([0-9]*\).*/\1/p')
  cpu=$(tail -1 /tmp/e_$set.log | sed 's/.*cpu \([0-9.]*\)s/\1/')
  echo "  $set sim=$sim cpu=$cpu miss=$miss"
done
