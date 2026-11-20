IMAGE ?= blast-radius-entry
DOCKER ?= docker
.DEFAULT_GOAL := build
SAMPLE := $(CURDIR)/sample
OUTPUT := $(CURDIR)/output.jsonl

# The flags the scorer runs your image with. The scorer is linux/amd64, so on
# an Apple Silicon Mac --platform makes your local run match it.
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: build sample check profile

sample/tests.json:
	python3 tools/fetch_sample.py

build:
	$(DOCKER) build --platform linux/amd64 --tag $(IMAGE) .

# Runs your image once on the sample commits under the scorer's limits and
# scores what it printed.
sample: sample/tests.json build
	$(DOCKER) run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# The output's rules only: every line well formed and every commit answered.
# A wrong selection still passes here; make sample counts the misses.
check: sample/tests.json build
	$(DOCKER) run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) > /dev/null && echo "format ok"

# Compare fresh interpreter runs under the same restrictions as the sample run.
profile: sample/tests.json build
	@set -e; for run in 1 2 3; do \
		echo "Run $$run"; \
		report=$$($(DOCKER) run $(RUN_FLAGS) --volume "$(CURDIR)/tools/profile_selector.py:/bench.py:ro" \
			$(IMAGE) python /bench.py 2>&1 > $(OUTPUT)) || { echo "$$report" >&2; exit 1; }; \
		echo "$$report" >&2; \
		cpu=$$(printf '%s\n' "$$report" | sed -n 's/^Selector CPU seconds: //p'); \
		test "$$(wc -c < $(OUTPUT))" -le 2097152; \
		python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) --cpu-seconds "$$cpu"; \
	done
