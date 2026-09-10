.PHONY: next-version

# The smallest version the next release may carry, from the changelog.d fragments since the last tag (RELEASING.md).
next-version:
	python3 scripts/next_version.py
