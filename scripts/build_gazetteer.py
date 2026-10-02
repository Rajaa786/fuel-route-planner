"""Build ``data/us_places.csv.gz`` from the GeoNames ``US.txt`` dump.

Dev-only, one-off tool: the generated file is committed, so the application never
needs network access (or GeoNames) to geocode. Re-run only to refresh the data.

    curl -LO https://download.geonames.org/export/dump/US.zip && unzip US.zip
    uv run python scripts/build_gazetteer.py US.txt

GeoNames data is licensed CC BY 4.0 (https://www.geonames.org/).
"""

import argparse
import csv
import gzip
import io
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from domain.normalization import US_STATES, normalize_place_name

OUTPUT = Path(__file__).resolve().parent.parent / "data" / "us_places.csv.gz"

# Historical / abandoned / destroyed places are not valid trip endpoints.
EXCLUDED_FEATURE_CODES = {"PPLH", "PPLQ", "PPLW", "PPLCH"}
# Lower is better. Sections of a city (PPLX) must never shadow the city itself.
FEATURE_RANK = {"PPLC": 0, "PPLA": 1, "PPLA2": 2, "PPLA3": 3, "PPLA4": 3, "PPL": 4, "PPLX": 6}
DEFAULT_FEATURE_RANK = 5
# Alternate names ("NYC", "New York") are only trusted for sizeable places; for
# hamlets they are mostly noise that would shadow real towns.
ALIAS_MIN_POPULATION = 50_000

# A key is "ambiguous" when a same-name place in the same state lies further than
# this from the chosen one and population cannot tell them apart.
AMBIGUITY_DISTANCE_MILES = 10.0
AMBIGUITY_POPULATION_RATIO = 5


def miles_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((phi2 - phi1) / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * 3958.7613 * math.asin(math.sqrt(a))


def read_candidates(source: Path) -> dict[tuple[str, str], list[tuple]]:
    """Group every populated place by its ``(normalised name, state)`` key."""
    candidates: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    with source.open(encoding="utf-8") as handle:
        for line in handle:
            cols = line.rstrip("\n").split("\t")
            feature_class, feature_code, state = cols[6], cols[7], cols[10]
            if feature_class != "P" or feature_code in EXCLUDED_FEATURE_CODES:
                continue
            if state not in US_STATES:
                continue

            name, ascii_name = cols[1], cols[2]
            population = int(cols[14] or 0)
            rank = FEATURE_RANK.get(feature_code, DEFAULT_FEATURE_RANK)
            latitude, longitude = round(float(cols[4]), 5), round(float(cols[5]), 5)

            names = {(name, True), (ascii_name, True)}
            if population >= ALIAS_MIN_POPULATION and cols[3]:
                names.update((alias, False) for alias in cols[3].split(",") if alias.isascii())

            for candidate, is_primary in names:
                key = normalize_place_name(candidate)
                if len(key) < 2:
                    continue
                # Preference: primary name > larger population > better feature rank.
                score = (is_primary, population, -rank)
                place = (score, ascii_name, latitude, longitude, population)
                candidates[(key, state)].append(place)
    return candidates


def resolve(candidates: list[tuple]) -> tuple[str, float, float, int, bool]:
    """Pick the best place for a key and say whether that pick is a coin toss."""
    winner = max(candidates, key=lambda candidate: candidate[0])
    _, name, latitude, longitude, population = winner
    ambiguous = any(
        miles_between(latitude, longitude, other[2], other[3]) > AMBIGUITY_DISTANCE_MILES
        and (population == 0 or population < AMBIGUITY_POPULATION_RATIO * other[4])
        for other in candidates
    )
    return name, latitude, longitude, population, ambiguous


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="Path to the extracted GeoNames US.txt")
    args = parser.parse_args()

    candidates = read_candidates(args.source)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    ambiguous_keys = 0
    # mtime=0 keeps the output byte-identical across runs (no spurious git diffs).
    with (
        OUTPUT.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed,
        io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text,
    ):
        writer = csv.writer(text, lineterminator="\n")
        writer.writerow(
            ["key", "state", "name", "latitude", "longitude", "population", "ambiguous"]
        )
        for (key, state), options in sorted(candidates.items()):
            name, latitude, longitude, population, ambiguous = resolve(options)
            ambiguous_keys += ambiguous
            writer.writerow([key, state, name, latitude, longitude, population, int(ambiguous)])
    print(
        f"Wrote {len(candidates):,} place keys ({ambiguous_keys:,} ambiguous) to {OUTPUT} "
        f"({OUTPUT.stat().st_size / 1e6:.1f} MB)"
    )


if __name__ == "__main__":
    main()
