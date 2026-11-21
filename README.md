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

## Sources

The Docker base image, Makefile run limits, scorer, scoring specification, dependency lock, and starter scheduling baseline come from the [official RWX kit](https://www.joinbuilders.org/buildathon/tracks/blast-radius). Development milestones and reproducible measurements are recorded in `PROGRESS.md`.

The image fetches the MIT-licensed [SQLGlot base revision](https://github.com/tobymao/sqlglot/tree/eb11aec4be8cb2122afe713a4816662c3f0fb2fa) and keeps its license in `/app/base/LICENSE`. Offline map generation also uses pytest and Coverage.py. The submitted selector uses the Python standard library. See [map reproduction](maps/README.md) for the dependency versions and commands.

## Publish a practice entry

Create an empty public GitHub repository. From this checkout, replace `PUBLIC_GITHUB_URL` with its HTTPS or SSH clone URL and run:

```sh
git remote add origin PUBLIC_GITHUB_URL
git push -u origin HEAD:main
```

Enter that repository's URL and the verified candidate SHA from `PROGRESS.md` on the [RWX track page](https://www.joinbuilders.org/buildathon/tracks/blast-radius). Keep the repository public and the submitted commit available through the results.
