.PHONY: next-version

# The smallest version the next release may carry, from the changelog entries since the last tag (RELEASING.md).
# `python3 scripts/next_version.py --defer "<why>"` persists a skipped minimum in changelog.d/.deferred_minimum.
next-version:
	python3 scripts/next_version.py
