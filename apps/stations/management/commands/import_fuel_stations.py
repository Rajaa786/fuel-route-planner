from itertools import batched
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.stations.importer import clean_and_geocode, read_rows
from apps.stations.index import reset_station_index
from apps.stations.models import FuelStation, Place

BATCH_SIZE = 2_000
UPDATE_FIELDS = [
    "name",
    "address",
    "city",
    "state",
    "rack_id",
    "retail_price",
    "latitude",
    "longitude",
]


class Command(BaseCommand):
    help = (
        "Import the fuel-price CSV: clean it, merge duplicate OPIS IDs, geocode each "
        "station offline against the Place gazetteer and upsert FuelStation rows."
    )

    def add_arguments(self, parser):
        parser.add_argument("--path", type=Path, default=settings.FUEL_PRICES_PATH)

    def handle(self, *args, path: Path, **options):
        if not path.exists():
            raise CommandError(f"Fuel price file not found: {path}")
        if not Place.objects.exists():
            raise CommandError("The gazetteer is empty. Run `manage.py load_places` first.")

        # Ambiguous places are left out on purpose: see Place.ambiguous.
        places = {
            (key, state): (latitude, longitude)
            for key, state, latitude, longitude in Place.objects.filter(ambiguous=False)
            .values_list("key", "state", "latitude", "longitude")
            .iterator()
        }
        report = clean_and_geocode(read_rows(path), places)
        if not report.stations:
            raise CommandError("No usable stations found; refusing to empty the table.")

        # Upsert + prune in one transaction, so the table always mirrors the file.
        with transaction.atomic():
            for batch in batched(report.stations, BATCH_SIZE):
                FuelStation.objects.bulk_create(
                    [
                        FuelStation(
                            opis_id=s.opis_id,
                            name=s.name,
                            address=s.address,
                            city=s.city,
                            state=s.state,
                            rack_id=s.rack_id,
                            retail_price=s.retail_price,
                            latitude=s.latitude,
                            longitude=s.longitude,
                        )
                        for s in batch
                    ],
                    update_conflicts=True,
                    unique_fields=["opis_id"],
                    update_fields=UPDATE_FIELDS,
                )
            current_ids = {s.opis_id for s in report.stations}
            stale_ids = set(FuelStation.objects.values_list("opis_id", flat=True)) - current_ids
            for batch in batched(sorted(stale_ids), 500):  # stay under SQLite's variable limit
                FuelStation.objects.filter(opis_id__in=batch).delete()
        reset_station_index()

        self.stdout.write(
            f"Rows read: {report.rows_read:,} | outside USA: {report.rows_outside_usa:,} | "
            f"invalid: {report.rows_invalid:,} | duplicate quotes merged: "
            f"{report.duplicate_rows_merged:,} | removed stale stations: {len(stale_ids):,}"
        )
        if report.stations_not_geocoded:
            self.stdout.write(
                self.style.WARNING(
                    f"Skipped {len(report.stations_not_geocoded)} station(s) whose city is missing "
                    "from the gazetteer or ambiguous within its state (a misplaced station is "
                    "worse than a missing one). Use -v 2 to list them."
                )
            )
            if options["verbosity"] >= 2:
                self.stdout.write("\n".join(report.stations_not_geocoded))
        self.stdout.write(self.style.SUCCESS(f"Imported {len(report.stations):,} fuel stations."))
        self.stdout.write(
            "Note: a running server keeps its in-memory station index; restart it to pick "
            "up this import."
        )
