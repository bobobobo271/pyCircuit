PYTHON ?= python3

.PHONY: check lint
check:
	$(PYTHON) -m pytest tests/test_coarse.py -v
lint:
	$(PYTHON) -m ruff check models/coarse tools/build_coarse_machine.py tools/build_coarse_rtl.py src/wse_model/programs/coarse.py src/wse_model/programs/coarse_allgather.py tests/test_coarse.py verification/coarse_cases.py
