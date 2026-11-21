#!/bin/sh
# Trace every test in its own fresh interpreter. usage: iso_run.sh <tests.json> <outdir> <parallel> [limit]
mkdir -p $2
python -c "
import json,sys
T=json.load(open('$1'))['tests']
lim=int(sys.argv[1]) if len(sys.argv)>1 else len(T)
for t in T[:lim]: print(t['number'], t['id'])
" $4 > /tmp/ids.txt
cd /sqlglot
cat /tmp/ids.txt | xargs -P $3 -L 1 sh -c '[ -s "$0/$1.json" ] || ISOOUT="$0/$1.json" PYTHONPATH=/sqlglot:/work timeout 900 python -m pytest -q -p isoplug -p no:cacheprovider "$2" > /dev/null 2>&1; [ -s "$0/$1.json" ] || echo "FAIL $1 $2"' $2
echo done $(ls $2 | wc -l)
