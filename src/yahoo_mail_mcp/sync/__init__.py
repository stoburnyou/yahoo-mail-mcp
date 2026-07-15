"""Future sync adapters (e.g. Notion).

An adapter here should read/write the `decisions` table in store/db.py:
push sender groups out for review, pull decision tags back in. The core
pipeline is intentionally unaware of any external review surface.
"""
