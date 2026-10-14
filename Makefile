# The board lives at http://127.0.0.1:$(PORT). Override either on the command line,
# e.g. `make run BACKLOG=~/other-backlog PORT=8800`.
BACKLOG ?= $(HOME)/Library/CloudStorage/Dropbox/Docs/Projects/Computational/backlog
PORT    ?= 8765
PYTHON  ?= python3

.PHONY: run

run:
	$(PYTHON) wbl.py --root "$(BACKLOG)" serve --port $(PORT) --open
