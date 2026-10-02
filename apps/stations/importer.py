"""Cleaning and geocoding of the fuel-price CSV.

Kept apart from the management command so the rules can be unit-tested without
touching the filesystem or stdout.
"""

import csv
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from domain.normalization import US_STATES, normalize_place_name

# Fuel is priced to a tenth of a cent; the file's 8-decimal values are rounded to that.
PRICE_QUANTUM = Decimal("0.001")
# Sanity bounds (USD/gal); also what FuelStation.retail_price can store.
MIN_PRICE, MAX_PRICE = Decimal("0.001"), Decimal("99.999")


@dataclass(frozen=True, slots=True)
class CleanStation:
    opis_id: int
    name: str
    address: str
    city: str
    state: str
    rack_id: int | None
    retail_price: Decimal
    latitude: float
    longitude: float


@dataclass(slots=True)
class ImportReport:
    rows_read: int = 0
    rows_invalid: int = 0
    rows_outside_usa: int = 0
    duplicate_rows_merged: int = 0
    stations_not_geocoded: list[str] = field(default_factory=list)
    stations: list[CleanStation] = field(default_factory=list)


def read_rows(path: Path) -> list[dict[str, str]]:
    # utf-8-sig: tolerate the BOM that Excel adds when exporting CSV.
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def clean_and_geocode(
    rows: Iterable[Mapping[str, str]], places: Mapping[tuple[str, str], tuple[float, float]]
) -> ImportReport:
    """Turn raw CSV rows into one geocoded station per OPIS ID.

    ``places`` maps ``(normalised city, state)`` to ``(latitude, longitude)``.

    Rules:

    * whitespace is stripped (a sixth of the file has padded city names);
    * rows outside the United States (620 Canadian truck stops) are dropped: the
      brief is US-only and the offline gazetteer only covers US places;
    * rows sharing an OPIS ID are the same physical truck stop (same name, address
      and rack) quoted at several prices; they are merged and priced at the
      **lowest** quote, i.e. the cheapest fuel on sale there. That keeps every
      price traceable to a row of the source file.
    """
    report = ImportReport()
    grouped: dict[int, list[tuple[Mapping[str, str], Decimal]]] = defaultdict(list)

    for raw in rows:
        report.rows_read += 1
        # DictReader yields None for cells missing from a short line.
        row = {key: (value or "").strip() for key, value in raw.items() if key}
        state = row.get("State", "").upper()
        if state not in US_STATES:
            report.rows_outside_usa += 1
            continue
        try:
            opis_id = int(row["OPIS Truckstop ID"])
            price = Decimal(row["Retail Price"]).quantize(PRICE_QUANTUM, ROUND_HALF_UP)
        except (KeyError, ValueError, InvalidOperation):
            report.rows_invalid += 1
            continue
        if not price.is_finite() or not MIN_PRICE <= price <= MAX_PRICE:
            report.rows_invalid += 1
            continue
        if opis_id <= 0 or not row.get("City"):
            report.rows_invalid += 1
            continue
        grouped[opis_id].append((row, price))

    for opis_id, quotes in sorted(grouped.items()):
        row = quotes[0][0]
        city, state = row["City"], row["State"].upper()
        report.duplicate_rows_merged += len(quotes) - 1

        coordinates = places.get((normalize_place_name(city), state))
        if coordinates is None:
            report.stations_not_geocoded.append(f"{opis_id}: {city}, {state}")
            continue

        rack_id = row.get("Rack ID", "")
        report.stations.append(
            CleanStation(
                opis_id=opis_id,
                name=" ".join(row.get("Truckstop Name", "").split()),
                address=" ".join(row.get("Address", "").split()),
                city=city,
                state=state,
                rack_id=int(rack_id) if rack_id.isdigit() else None,
                retail_price=min(price for _, price in quotes),
                latitude=coordinates[0],
                longitude=coordinates[1],
            )
        )
    return report
