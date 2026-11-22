"""AnyCompany Claims voice partner bridge (A2A + Nova 2.5 Sonic).

The bridge reuses the Starter_Kit's ``a2a_connect`` package for protocol frame
building, tracing and fragment reassembly (task 3.7); importing this package
puts the kit on the import path (see ``_kitpath``).
"""
from ._kitpath import ensure_kit_importable

ensure_kit_importable()
