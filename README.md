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
   - the selection is packed longest-first over the worker count (1-8) with the lowest simulated cost.

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
