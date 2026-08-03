"""LangChain document loader for HexRead: one Document per page, converted to Markdown.

    docs = HexReadLoader("report.pdf").load()

Needs a paid HexRead plan and HEXREAD_API_KEY.
"""

from __future__ import annotations

from .loader import HexReadLoader

__version__ = "0.1.0"

__all__ = ["HexReadLoader", "__version__"]
