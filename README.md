# RWX Blast Radius

Select and schedule SQLGlot tests for independent patches against commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. The Docker entry point reads the test manifest and patches under `/data` and writes one JSON object per commit. The local scorer reads the sample answers separately.

## Reproduce a run

With Docker, GNU Make and Python installed, run `make sample`. To evaluate the same output format directly with Python, use these commands in a shell with UTF-8 redirection (e.g. Bash or PowerShell 7.4+).

```sh
python tools/fetch_sample.py
git init .work/sqlglot
git -C .work/sqlglot fetch --depth=1 https://github.com/tobymao/sqlglot.git eb11aec4be8cb2122afe713a4816662c3f0fb2fa
git -C .work/sqlglot checkout --detach FETCH_HEAD
python selector.py sample --base .work/sqlglot > output.jsonl
python score.py sample sample/answers.json output.jsonl
```

The sample download verifies the official archive checksum. Sample patches and labels stay outside the submission image and Git history. The scorer reports simulated time and the number of commits with missed failures. The competition separately charges the CPU time of the selector.

Run `make profile` for three fresh container runs with the scorer's limits. Each run prints local selector CPU time to stderr, checks the output byte limit and evaluates the JSONL with the official scorer. Server CPU timing depends on the scoring machine.

## Selection

The selector validates the diff against pinned source, maps affected function bodies to measured test coverage, and includes tests that read a changed fixture or source file. Changes it cannot attribute to tests use the full catalog. The scheduler tries one through eight workers using integer-millisecond durations and charges the worker overhead from the official scorer.

For edits confined to comment or blank lines, the selector also checks Python tokens, syntax, directives and compiled code. When the checks establish equivalent executable code after accounting for line positions, it keeps tests that read the file, own the test module or inspect source positions, as well as tests with incomplete coverage. Each other file in a mixed patch still contributes its dependencies. Dynamic source inspection outside the measured baseline remains a prediction risk.

For table callbacks and same-shaped class settings, it uses measured handler calls, configuration reads and dependencies created during class initialization. It can attribute an isolated condition edit to the tests that evaluated that line. For supported identity comparisons, it checks the requested operator against observed baseline operands from the TPC-DS executor test. The map generation commands are in [maps/README.md](maps/README.md) and [maps/dispatch.md](maps/dispatch.md).

For changes to boolean values in an expression class's literal `arg_types` dictionary, the selector can use a baseline map of tests that consume those values or the derived `required_args` set. The map follows inheritance and schema copies. For an isolated change, startup validation can preserve the same outcome when all affected arguments were populated on every observed call. Changes to the keys and unverified startup readers retain the existing fallback. [Schema map reproduction](maps/README.md#expression-argument-schemas)

For isolated changes to required arguments, the collector also records which arguments were populated on each numbered test's calls to the validator. A test can leave the schedule if every observed read confirms the same validation outcome. [Per-test schema observations](maps/README.md#expression-argument-schemas)

For a literal setting on a concrete SQL dialect, the selector can use tests that read that setting directly or through inheritance. The map distinguishes a lookup of one class dictionary key from enumeration of the whole dictionary. [Scalar map reproduction](maps/README.md#literal-settings-on-dialect-classes)

## Sources

The Docker base image, Makefile run limits, scorer, scoring specification, dependency lock, and starter scheduling baseline come from the [official RWX kit](https://www.joinbuilders.org/buildathon/tracks/blast-radius). Development milestones and reproducible measurements are recorded in `PROGRESS.md`.

The image fetches the MIT-licensed [SQLGlot base revision](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa) and keeps its license in `/app/base/LICENSE`. Offline map generation also uses pytest and Coverage.py. The submitted selector uses the Python standard library. See [map reproduction](maps/README.md) for the dependency versions and commands.

## Practice entry

Use `https://github.com/Borovail/rwx-blast-radius` in the repository field. Leave the optional commit field empty to score the latest version of the default branch, or enter the full SHA to select a particular revision. [RWX submission page](https://www.joinbuilders.org/buildathon/tracks/blast-radius)
