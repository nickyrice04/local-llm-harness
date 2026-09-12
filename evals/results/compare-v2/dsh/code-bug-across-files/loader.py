"""Load an inventory export (CSV) into Item records."""

import csv

from inv.models import Item


def _int(value):
    """Integers arrive as text; anything else is left as-is for the caller."""
    value = value.strip()
    return int(value) if value.isdigit() else value


def _float(value):
    return float(value.replace(",", "")) if value.replace(",", "").replace(".", "").isdigit() else value


def load(path):
    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [Item(sku=row["sku"], qty=_int(row["qty"]), price=_float(row["price"])) for row in rows]
