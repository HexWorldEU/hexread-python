"""HexRead for LlamaIndex: PDFs and images in, Markdown Documents out.

    docs = HexReadReader().load_data("report.pdf")

Needs a paid HexRead plan and HEXREAD_API_KEY.
"""

from __future__ import annotations

from .base import HexReadReader

__version__ = "0.1.0"

__all__ = ["HexReadReader", "__version__"]
