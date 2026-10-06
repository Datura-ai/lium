"""Which GPU generations a template's image can drive, derived from its image name and tag.

Nothing in the template record says "Hopper" or "Blackwell"; the CUDA build in
the tag does. Blackwell (B200, B300, RTX PRO 6000, RTX 50x0; compute capability
10.x/12.x) needs CUDA 12.8 or newer. Hopper (H100/H200) works from CUDA 11.8.
"""

import re
from typing import Any, Dict, Optional

from lium.sdk import Template

# cu126, cu130, cuda12.6, cuda-12.6, cuda_12.6, CUDA 12.6, cuda13.0.2 — a bare leading "12.6.0-…" is not read
_CUDA = re.compile(r"(?:cu(?:da)?[-_ ]?)(1[1-9])[.]?(\d)(?:\.\d+)?(?![\d.])", re.IGNORECASE)
_TORCH_IN_TAG = re.compile(r"torch[-_:]?(\d+\.\d+\.\d+)", re.IGNORECASE)
_LEADING_VERSION = re.compile(r"^(\d+\.\d+\.\d+)(?=[-_+]|$)")

BLACKWELL_MIN_CUDA = 12.8
HOPPER_MIN_CUDA = 11.8

HOPPER_AND_BLACKWELL = "hopper+blackwell"
HOPPER_ONLY = "hopper"
PRE_HOPPER = "pre-hopper"

ARCH_CHOICES = ("hopper", "blackwell")


def cuda_version(image: Optional[str], tag: Optional[str]) -> Optional[float]:
    """``12.8`` from ``...-cuda12.8-...`` or ``cu128``; tag first, then image name."""
    for text in (tag or "", image or ""):
        match = _CUDA.search(text)
        if match:
            return float(f"{match.group(1)}.{match.group(2)}")
    return None


def torch_version(image: Optional[str], tag: Optional[str]) -> Optional[str]:
    """``2.12.0`` from ``...-torch2.12.0-...`` anywhere in the tag, or from the leading
    version of a pytorch image's tag (``pytorch/pytorch:2.7.1-cuda12.8-...``); None otherwise."""
    image = image or ""
    tag = tag or ""
    explicit = _TORCH_IN_TAG.search(tag)
    if explicit:
        return explicit.group(1)
    if "torch" in image.lower():
        leading = _LEADING_VERSION.match(tag)
        if leading:
            return leading.group(1)
    return None


def arch_support(cuda: Optional[float]) -> Optional[str]:
    if cuda is None:
        return None
    if cuda >= BLACKWELL_MIN_CUDA:
        return HOPPER_AND_BLACKWELL
    if cuda >= HOPPER_MIN_CUDA:
        return HOPPER_ONLY
    return PRE_HOPPER


def supports(arch: str, support: Optional[str]) -> bool:
    """Does a template with ``support`` run on ``arch`` (``hopper`` or ``blackwell``)?"""
    if support is None:
        return False
    if arch == "blackwell":
        return support == HOPPER_AND_BLACKWELL
    if arch == "hopper":
        return support in (HOPPER_AND_BLACKWELL, HOPPER_ONLY)
    raise ValueError(f"Unknown architecture '{arch}'; use one of {', '.join(ARCH_CHOICES)}")


def describe(template: Template) -> Dict[str, Any]:
    """The derived fields for JSON and the table: ``cuda_version``, ``torch_version``, ``arch``."""
    cuda = cuda_version(template.docker_image, template.docker_image_tag)
    return {
        "cuda_version": cuda,
        "torch_version": torch_version(template.docker_image, template.docker_image_tag),
        "arch": arch_support(cuda),
    }


def arch_label(support: Optional[str]) -> str:
    return {
        HOPPER_AND_BLACKWELL: "Hopper+Blackwell",
        HOPPER_ONLY: "Hopper only",
        PRE_HOPPER: "pre-Hopper",
        None: "?",
    }[support]


def runs_on_cell(cuda: Optional[float], support: Optional[str]) -> str:
    """The table cell: the CUDA build first, then the generations — ``13.0 Hopper+Blackwell``; ``?`` when the tag says nothing."""
    if cuda is None:
        return "?"
    return f"{cuda:.1f} {arch_label(support)}"
