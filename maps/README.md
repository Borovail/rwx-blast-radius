The coverage map is measured only on SQLGlot baseline
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, without candidate patches or patch
outcomes. It uses the benchmark manifest's numbered test IDs. Source text is read
from the separately acquired pinned checkout at runtime; upstream source is not
vendored here.

`coverage.json.gz` uses schema version 1. Each `paths[path]` record contains a
normalized source SHA-256, line count, executed line-to-test-number mapping,
function bounds and body coverage, startup/shared-fixture execution, and audited
file readers. `context` records collection and outcome completeness, ignored
unnumbered platform skips, dependency versions, and process execution evidence.
Tests in `context.untraced_tests` are mandatory supplements for every patch.
Missing or invalid maps, incomplete collection/execution, or unknown startup
execution require selecting the entire test manifest.

Rebuild with Python 3.13 and the official locked test dependencies plus
`coverage==7.17.4`. Use the benchmark's exact Python image so its SQLite version
and skip conditions match:

```sh
python tools/fetch_sample.py
git init .work/sqlglot
git -C .work/sqlglot remote add origin https://github.com/tobymao/sqlglot.git
git -C .work/sqlglot fetch --depth 1 origin eb11aec4be8cb2122afe713a4816662c3f0fb2fa
git -C .work/sqlglot checkout --detach FETCH_HEAD
python tools/build_coverage.py --repo .work/sqlglot \
  --write-checkout-metadata .work/base_checkout.json
```

The metadata command uses Git to verify the clean baseline and records hashes of
every tracked regular file. `--checkout-metadata` checks those exact file hashes
inside the slim container, which does not include Git. Prepare Linux cp313
dependencies in `.work/linux-packages` using the official lock as constraints:

```sh
mkdir -p .work/linux-packages
docker run --rm \
  -v "$PWD/requirements.lock:/constraints.txt:ro" \
  -v "$PWD/.work/linux-packages:/out" \
  python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 \
  pip install --target /out --constraint /constraints.txt \
    pytest pytest-timeout pytest-xdist numpy pandas duckdb python-dateutil pytz \
    setuptools setuptools-scm coverage==7.17.4
```

Then run with the source and dependency mounts read-only and networking disabled:

```sh
mkdir -p maps
docker run --rm --network none --cpus 2 \
  -e PYTHONPATH=/opt/deps -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHON_CPU_COUNT=2 -e OPENBLAS_NUM_THREADS=1 -e OMP_NUM_THREADS=1 \
  -v "$PWD/.work/sqlglot:/repo:ro" \
  -v "$PWD/tools:/tools:ro" \
  -v "$PWD/.work/base_checkout.json:/checkout.json:ro" \
  -v "$PWD/sample/tests.json:/tests.json:ro" \
  -v "$PWD/.work/linux-packages:/opt/deps:ro" \
  -v "$PWD/maps:/out" \
  python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 \
  python /tools/build_coverage.py --repo /repo \
    --checkout-metadata /checkout.json --tests-manifest /tests.json \
    --output /out/coverage.json.gz
```

`SKIP_INTEGRATION=0` is set by the generator. CPU and BLAS limits bound worker
counts without changing SQLGlot source or test assertions. Run the generator in a
fresh interpreter. A nonzero exit status means the generated map is diagnostic
and must not be used for narrowed selection. An optional `--sources-output` writes
a local source snapshot for inspection; keep it outside the submitted repository.

### Include execution in child processes

Keep the first-pass map and run each supplemental test in a fresh interpreter.
The tool records Python line events and file reads for the test process and its
children. It merges observations only after checking the complete pytest
protocol and matching every observed process start to a completed trace.
Unverified tests remain in the supplemental set.

```sh
mkdir -p .work/refinement
cp maps/coverage.json.gz .work/refinement/baseline.json.gz
docker run --rm --network none --cpus 2 \
  -e PYTHONPATH=/opt/deps -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD/.work/sqlglot:/repo:ro" -v "$PWD/tools:/tools:ro" \
  -v "$PWD/sample/tests.json:/tests.json:ro" \
  -v "$PWD/.work/linux-packages:/opt/deps:ro" \
  -v "$PWD/.work/refinement:/out" \
  python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 \
  python /tools/refine_coverage.py --repo /repo --manifest /tests.json \
    --coverage /out/baseline.json.gz --observations /out/isolated \
    --output /out/full-coverage.json.gz --max-tests 1620 --workers 2
```

Inspect the per-test results in `.work/refinement/full-coverage.json.gz.report.json`
before copying the refined map to `maps/coverage.json.gz`. The shipped map
incorporates 25 isolated tests across 98 processes. Its SHA-256 is
`74da323cb0a893cac0284dc9f35fefb2040944bf5cfa01a46a614933858c3a59`.

After rebuilding coverage, regenerate the source and line-position observer
index with the pinned checkout and the same coverage file.

```sh
python tools/build_position_index.py --repo .work/sqlglot --coverage maps/coverage.json.gz --output maps/positions.json
```

The generator verifies the source hashes and intersects source-inspection sites
with measured test and startup coverage. The runtime enables comment selection
when the index matches the coverage digest and all observed sites can be
attributed to individual tests.

For more precise attribution of code-object reads, run
`python tools/build_observer_targets.py --repo .work/sqlglot --coverage maps/coverage.json.gz --observations .work/code-observations --output maps/observer_targets.json`
under the pinned Linux image with the locked test dependencies. The tool reruns
the tests identified by the position index. It checks their outcome and matches
executed attribute loads to audit events before recording observed source files.

`body.json.gz` contains bounded baseline operands, identity relationships and
observed results for identity comparisons reached by the 21.454-second TPC-DS
executor test and its child processes. That test determined the runtime of
several baseline schedules. The selector evaluates a changed comparison against
these observations at runtime. Unsupported operand types and missing observations
retain ordinary coverage.

```sh
python tools/build_body_index.py --repo .work/sqlglot --manifest sample/tests.json \
  --coverage maps/coverage.json.gz --observations .work/body-observations \
  --test-number 1106 --operators Is,IsNot --output maps/body.json.gz
```

Use the pinned Linux Python and test dependencies for this command. All Python
descendants must finish and produce complete observations before their test can
contribute to the index.

The recorded run exercised a broader set of comparison operators. The shipped
map retains only baseline identity comparisons and contains 294 sites across
36 files. The command above collects those sites directly.

### Expression argument schemas

`schema.json.gz` records the tests that read values from an expression class's
`arg_types` mapping or its derived `required_args` set. Looking up whether a
key exists and iterating over keys do not consume the stored boolean. The
collector follows class inheritance and schema copies. A startup reader can
make a provider unsafe to narrow. For the pinned validator's loop over required
arguments, the collector records which arguments were populated at every
observed startup call. If any original required argument was missing, the
collector declines that observation because changing set membership could
change the order of the errors. Unrecognized consumers also retain the fallback.

An isolated literal flag edit can use the ordinary test readers when every
changed flag was populated in every relevant startup validation. The selector
checks this intersection across processes, inherited providers and schema
copies. Other edits keep their original selection.

The same collector records observations for each numbered test. A test can
leave the schedule when every reachable reader of the edited schema executed
the pinned `Expression.error_messages` validator, and all changed arguments
were populated on every invocation. An opaque read by that test preserves its
place in the schedule. The collector checks the complete method's AST,
intersects observations across processes, and accounts for shared process
pools. File owners and explicit file readers remain in the final schedule.

Use the pinned Python image, read-only SQLGlot checkout and locked dependency
directory described above. Choose a fresh observation directory for each run.
From the root of this project on Linux:

```sh
mkdir -p .work/schema-collection
docker run --rm --network none --cpus 2 \
  -e PYTHONPATH=/deps -e PYTHONDONTWRITEBYTECODE=1 -e PYTHON_CPU_COUNT=2 \
  -e OPENBLAS_NUM_THREADS=1 -e OMP_NUM_THREADS=1 \
  -v "$PWD:/project:ro" -v "$PWD/.work/sqlglot:/repo:ro" \
  -v "$PWD/.work/linux-packages:/deps:ro" -v "$PWD/.work/schema-collection:/out" \
  -w /project \
  python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 \
  python tools/build_schema_coverage.py --repo /repo --manifest sample/tests.json \
    --checkout-metadata .work/base_checkout.json --coverage maps/coverage.json.gz \
    --observations /out/observations --output /out/schema.json.gz
```

The tool checks baseline file hashes, pytest collection, numbered outcomes and
child-process observations. Inspect the resulting `context` before copying the
completed artifact into `maps/`.

### Literal settings on dialect classes

`scalars.json.gz` attributes reads of public boolean, integer and string settings
to individual tests. The generator tracks normal and inherited attribute
access, class-local aliases, and reads from a class dictionary. Looking up
a fixed key in an ordinary class dictionary records the provider of that key.
Enumeration and dynamic keys keep their broader dependencies.

The generator inspects the pinned dialect metaclass and the inheritance paths
of concrete dialect classes. The selector consults these observations only
for a single literal replacement on an eligible declaration, with the same
value type and all surrounding source bytes preserved. It checks the source
hash, test manifest and both observation policies. Its result must be a subset
of the tests selected by the general dispatch map.

Use the same pinned image, dependencies, checkout and mount arguments as for
the schema map. In the container, with a fresh writable observation directory,
run the following command.

```sh
python tools/build_literal_coverage.py --repo /repo --manifest sample/tests.json \
  --checkout-metadata .work/base_checkout.json --coverage maps/coverage.json.gz \
  --observations /out/literal-observations --output /out/scalars.json.gz
```
