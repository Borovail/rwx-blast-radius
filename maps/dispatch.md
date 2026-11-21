`dispatch.json.gz` records class-configuration dependencies measured only on
SQLGlot baseline `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` and the supplied
numbered test manifest. Candidate patches and their outcomes are never inputs.

The import loader replaces attribute reads with equivalent `getattr` calls and
records the defining class in the actual method resolution order. It also
observes dynamic `getattr`, class dictionary inspection, and class constructors.
Initializer contexts connect reads to derived assignments and class use. This
captures inheritance, overrides, metaclasses, and `__init_subclass__` derivations
such as expression `required_args` without expression-specific rules.

The same loader runs in spawned Python interpreters through `sitecustomize` and
in forked children. Wrapped process starts must match completed observations.
Unknown process transitions, missing protocols, and collection errors prevent
complete artifacts. Shared-fixture or other unattributed startup reads block the
affected configuration and its dependents. Skipped numbered tests remain
mandatory supplements.

Use the pinned image, dependency directory, and checkout metadata described in
`README.md` in this directory. With mounts `/repo` (base), `/work` (this project),
`/opt/deps` (locked dependencies), and writable `/out`:

```sh
PYTHONPATH=/opt/deps PYTHONDONTWRITEBYTECODE=1 PYTHON_CPU_COUNT=2 \
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
python /work/tools/build_dispatch.py --repo /repo \
  --manifest /work/sample/tests.json \
  --checkout-metadata /work/.work/base_checkout.json \
  --coverage /work/maps/coverage.json.gz \
  --observations /out/observations --output /out/dispatch.json.gz
```

Run this in a fresh interpreter with no network and two CPUs. Observation
directories must be fresh. An isolated baseline rerun may repair a failed
instrumentation run; `--merge FIRST --merge REPLAY --output RESULT` unions the
dependencies and verifies that every numbered test has a passing or skipped
protocol in at least one input. Process and collection uncertainty cannot be
repaired by this union. Supply both raw directories with repeated
`--refresh-observations` and the pinned `--coverage` map when merging.

Shared process pools need additional attribution because a worker can outlive
its first numbered test. For a pool constructed without an individual test
context, measured dispatch sites define a conservative owner group. Reads
observed in its children are attributed to every numbered dispatcher in that
group. Independent per-test pools and unrelated processes keep their own reads.
If owner groups overlap, their union is used. This is derived from the existing
baseline process-site coverage, never from patch outcomes.

`m2_dispatch.DispatchSelector.from_file(path, tests)` accepts the full benchmark
manifest. Its `select(path, old_source, new_source)` returns a set of numbered
tests, or `None` when narrowing is unsupported. The runtime supports direct,
unconditional class assignments with same-shaped literal changes. Scalar types,
dictionary key sequences, and container shapes must stay fixed. Changed calls,
references, arithmetic, lambda bodies, added declarations, and other structural
changes use the caller's fallback. The complete old-source hash and the remaining
module AST must match. File readers and source-position observers are the
caller's responsibility.
