PY ?= .venv/bin/python
export PYTHONPATH := src

.PHONY: install lint fmt type test check migrate load sync smoke clean cache-tokenizers
install:            ## create venv + install pinned deps + tiktoken encodings
	uv venv -q -p 3.13 .venv && uv pip install -q -p .venv -e ".[dev,graph]"
	$(PY) -m nwb cache-tokenizers
cache-tokenizers:   ## download tokenizer encodings from config/graph.yaml (offline chunking afterwards)
	$(PY) -m nwb cache-tokenizers
lint:
	$(PY) -m ruff check src tests && $(PY) -m ruff format --check src tests
fmt:
	$(PY) -m ruff format src tests && $(PY) -m ruff check --fix src tests
type:
	$(PY) -m mypy
test:
	$(PY) -m pytest
check: lint type test   ## the gate: must be green before commit
migrate:
	$(PY) -m nwb migrate
load:               ## Layer 1 + Layer 2 from data/
	$(PY) -m nwb load-layer1 && $(PY) -m nwb load-layer2
sync:               ## drain graph_outbox into FalkorDB
	$(PY) -m nwb graph-sync
smoke: migrate load sync
clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache var
