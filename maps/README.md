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
