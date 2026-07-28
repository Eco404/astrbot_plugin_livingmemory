"""Backward-compatible import path for Topic fragment identity helpers."""

from ..topic_fragment_identity import (
    fragment_semantic_discriminator,
    logical_fragment_uid,
)

__all__ = ["fragment_semantic_discriminator", "logical_fragment_uid"]
