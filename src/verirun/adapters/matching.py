"""Normalised keys for matching names, GSTINs, and document references.

The adapter and the Verifier must agree on what "the same vendor" or "the same
purchase order" means. These helpers are deliberately small and shared: a
difference in punctuation, case, or whitespace is not a different vendor or PO.
"""

from __future__ import annotations

import re

NAME_NOISE = re.compile(r"[^a-z0-9]+")
WHITESPACE = re.compile(r"\s+")


def gstin_key(value: str) -> str:
    return WHITESPACE.sub("", value or "").upper()


def name_key(value: str) -> str:
    return NAME_NOISE.sub("", (value or "").casefold())


def reference_key(value: str) -> str:
    return name_key(value)
