#!/bin/sh
# For each patch in $1 (default sample): patched copy of sqlglot, full test run, failing ids into $2/<id>.txt
DIR=${1:-/work/sample/commits}
OUT=${2:-/work/truth}
mkdir -p $OUT
for p in $DIR/*.patch; do
  id=$(basename $p .patch)
  [ -s $OUT/$id.done ] && continue
  rm -rf /tmp/w /tmp/f && mkdir -p /tmp/f && cp -r /sqlglot /tmp/w && cd /tmp/w && patch -s -p1 < $p || echo "PATCHFAIL $id"
  FAILOUT=/tmp/f/fail PYTHONPATH=/tmp/w:/work timeout 900 python -m pytest -q -n 12 -p failplug -p no:cacheprovider tests > /tmp/f/log 2>&1
  cat /tmp/f/fail.* 2>/dev/null | sort -u > $OUT/$id.txt
  tail -1 /tmp/f/log > $OUT/$id.done
  echo "$id $(wc -l < $OUT/$id.txt) $(cat $OUT/$id.done)"
done
