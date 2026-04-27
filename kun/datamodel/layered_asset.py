"""Compatibility exports for the LayeredAsset data model.

The canonical implementation lives in ``kun.context.assets`` because asset
packing and storage are context-system concerns. This module keeps the
datamodel import path available for design docs and future callers.
"""

from kun.context.assets import AssetKind, AssetLayer, LayeredAsset, anonymize_text

__all__ = ["AssetKind", "AssetLayer", "LayeredAsset", "anonymize_text"]
