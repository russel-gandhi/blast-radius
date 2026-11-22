#!/bin/sh
cd /sqlglot
( for i in $(seq 0 21); do echo "tpch $i"; done; for i in $(seq 0 98); do echo "tpcds $i"; done ) | xargs -P 4 -L 1 sh -c '[ -s /work/tpccov/$0_$1.json ] || python /work/tpc_cov.py query $0 $1 /work/tpccov/$0_$1.json > /dev/null 2>&1; [ -s /work/tpccov/$0_$1.json ] || echo "FAIL $0 $1"'
echo done $(ls /work/tpccov | wc -l)
