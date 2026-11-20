# RWX Blast Radius

Select and schedule SQLGlot tests for independent patches against commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. The Docker entry point reads the test manifest and patches under `/data` and writes one JSON object per commit. The local scorer reads the sample answers separately.

## Reproduce a run

With Docker, GNU Make and Python installed, run `make sample`. To evaluate the same output format directly with Python, use these commands in a shell with UTF-8 redirection (e.g. Bash or PowerShell 7.4+).

```sh
python tools/fetch_sample.py
python selector.py sample > output.jsonl
python score.py sample sample/answers.json output.jsonl
```

The sample download verifies the official archive checksum. Sample patches and labels stay outside the submission image and Git history. The scorer reports simulated time and the number of commits with missed failures. The competition separately charges the CPU time of the selector.

Run `make profile` for three fresh container runs with the scorer's limits. Each run prints local selector CPU time to stderr, checks the output byte limit and evaluates the JSONL with the official scorer. Server CPU timing depends on the scoring machine.

## Sources

The Docker base image, Makefile run limits, scorer, scoring specification, dependency lock, and starter scheduling baseline come from the [official RWX kit](https://www.joinbuilders.org/buildathon/tracks/blast-radius). Development milestones and reproducible measurements are recorded in `PROGRESS.md`.
