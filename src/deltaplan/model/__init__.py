"""The domain model: frozen, slotted dataclasses holding tuples.

Nothing in here does I/O or imports the Databricks SDK. Every value is hashable,
so a whole table model can be fingerprinted.
"""
