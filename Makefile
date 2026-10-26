.DEFAULT_GOAL := help

PYTHON ?= python3
GEMTEXT_OUTPUT ?= $(HOME)/tmp/gemini-mailing-list
GEMTEXT_ROOT_URL ?= gemini://gemi.dev/gemini-mailing-list/

.PHONY: help reconstruct html gemtext

help:
	@printf '%s\n' \
	  'make reconstruct  Rebuild mbox files and XML from sources' \
	  'make html         Publish the reconstructed archive to www/' \
	  'make gemtext      Regenerate Gemtext, removing stale numbered pages' \
	  'Override GEMTEXT_OUTPUT and GEMTEXT_ROOT_URL to publish elsewhere.'

reconstruct:
	$(PYTHON) tools/reconstruct.py

html:
	$(PYTHON) tools/publish_html.py

# Validate before cleanup, then remove only files whose entire stem is numeric.
# This includes both thread pages and individual messages, at any digit width.
# Preserve about.gmi, downloads, subdirectories and other manually kept files.
gemtext:
	$(PYTHON) tools/publish_gemtext.py --output "$(GEMTEXT_OUTPUT)" --root-url "$(GEMTEXT_ROOT_URL)" --check
	@set -eu; \
	for page in "$(GEMTEXT_OUTPUT)"/[0-9]*.gmi "$(GEMTEXT_OUTPUT)"/messages/[0-9]*.gmi; do \
	  test -f "$$page" || continue; \
	  name=$${page##*/}; number=$${name%.gmi}; \
	  case "$$number" in *[!0-9]*) continue ;; esac; \
	  rm -f -- "$$page"; \
	done
	$(PYTHON) tools/publish_gemtext.py --output "$(GEMTEXT_OUTPUT)" --root-url "$(GEMTEXT_ROOT_URL)"
