# Blast Radius test selector

Entry for the RWX Blast Radius track: given a patch to sqlglot (pinned at
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`), choose the tests that can fail and split them across workers.

## How it works

1. **Coverage map** (`covmap.json.gz`, built once from the pinned commit by `tools/`):
   every sqlglot test is run under a `sys.monitoring` line tracer (`tools/covplug.py`), recording which
   source lines each test executes and which files (fixtures) it opens. Work the tests send to process
   pools is run in-process, with the same pickling round-trip, so it is credited to the right test.
   `tools/build_map.py` compresses this into line -> test-set tables.
2. **Selector** (`selector.py`), per patch:
   - applies the diff in memory to the pinned sources;
   - Python files whose AST is unchanged (comments, blank lines) select nothing;
   - changed statements inside functions select the tests that ran those lines;
   - import-time changes (class attributes, module constants, registries) select every test that ran
     code from that file or its dialect siblings (`dialects/`, `parsers/`, `generators/`, ...);
   - syntax errors and unknown files fall back to every test that could import them;
   - data files select the tests that opened them;
   - the selection is packed longest-first over the worker count (1-8) with the lowest simulated cost,
     and tests with weaker evidence (same function or file) fill spare time on the less busy workers,
     which costs nothing.
3. **Verification** (`verify.py`), when it pays: candidate tests are run against the patched tree
   (a symlink overlay of the pinned checkout in `/tmp`) in a forked child that already has numpy,
   pandas and duckdb imported. Tests that pass are dropped; failures, errors, failed subtests, skips,
   import errors and timeouts are kept. `selector.py` plans this per commit from each test's measured
   CPU (`testcost.json`) and class setup cost, and only checks tests when the expected CI seconds saved
   beat the CPU spent, within a wall-clock budget for the batch.
4. **Comparison outcomes** (`opmap/`, built by `tools/opplug.py`): on the pinned commit the suite runs
   once (twice, with two hash seeds) with every single comparison inside a sqlglot function rewritten to
   also record its outcome, what the related operators would have returned on the same operands, and
   the operands themselves when they are small primitives, per test. When a patch's only code change is
   one comparison's operator or constant, only the tests whose recorded outcome would flip are kept.
   Overflowing sites, unknown alternatives, outcomes seen at import time, or several code changes in one
   patch keep the coverage selection.
5. **Class-setting reads** (`attrmap.json.gz`, built by `tools/isoplug.py` with every test traced in its
   own fresh interpreter, so no cached dialect or tokenizer hides a dependency): every `X.NAME` read of
   a class-level setting in sqlglot is recorded per test with the class that owns it. A patch whose only
   code change is one setting keeps the tests that read it, unless it was read while modules were
   imported (its value may have been copied into derived data), its name appears as a string in
   sqlglot (getattr / `__dict__` reads), it is a dunder, or the class is an enum. Any class-level change
   also selects every test that read a setting of that class (its users).
6. **SQL-keyed table entries**: when a patch only changes entries of a `FUNCTIONS`, `KEYWORDS`,
   `*_PARSERS`, `*_MAPPING` (string keys) or `TRANSFORMS` (`exp.Foo` keys) table, the coverage
   selection is narrowed to tests whose method, class helpers, module code or SQL fixtures contain the
   key or the old/new value text (case-insensitive, whole words), tests that ran a line inside a sqlglot
   function containing that text, tests that name the table, and for class keys the users of that
   expression class. The text index is built when the image is built (`--build-text-index`).
7. **Replay** (`replay.py`) for the slow TPC-H/TPC-DS executor tests: instead of running them, each fixture
   query's DuckDB transpilation, its optimized and planned form after a pickle round trip, and the
   Python code generated for every plan step are digested and compared with digests recorded on the
   pinned commit (`replay_ref.json`), stopping at the first difference. Replay is only trusted when
   the patch leaves alone the code that runs during execution (`runtime_lines.json`), the code that
   runs while pickling expressions for process pools (`pickle_lines.json`), the tests and the data.

No per-patch answers are stored: the only precomputed data is the coverage/open map of the pinned commit.

## Rebuilding the map

```sh
docker build -t br-dev -f tools/Dockerfile.dev tools   # needs requirements.lock next to it
docker run --rm -v "$PWD/work:/work" -e PYTHONPATH=/sqlglot:/work br-dev pytest -q -p covplug -n 4 tests
python tools/build_map.py work
```

## Code and libraries

- Standard library only at runtime (Python 3.13).
- Map building uses pytest, pytest-xdist and the packages in `requirements.lock` (the track's environment).
- `score.py`, `Makefile`, `requirements.lock` and `sample/` come from the track's starter kit.

MIT licensed.
