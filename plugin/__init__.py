"""Hermes native plugin registration entry point."""

from .map_governance.native import register

__all__ = ["register"]
