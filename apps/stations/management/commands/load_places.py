import csv
import gzip
from itertools import batched
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.stations.models import Place

BATCH_SIZE = 5_000


class Command(BaseCommand):
    help = "Load the offline US gazetteer (data/us_places.csv.gz) into the Place table."

    def add_arguments(self, parser):
        parser.add_argument("--path", type=Path, default=settings.GAZETTEER_PATH)

    def handle(self, *args, path: Path, **options):
        if not path.exists():
            raise CommandError(f"Gazetteer file not found: {path}")

        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            places = [
                Place(
                    key=row["key"],
                    state=row["state"],
                    name=row["name"],
                    latitude=float(row["latitude"]),
                    longitude=float(row["longitude"]),
                    population=int(row["population"]),
                    ambiguous=row["ambiguous"] == "1",
                )
                for row in csv.DictReader(handle)
            ]

        # Full replace in one transaction: readers see the old table or the new one, never a mix.
        with transaction.atomic():
            Place.objects.all().delete()
            for batch in batched(places, BATCH_SIZE):
                Place.objects.bulk_create(batch)

        self.stdout.write(self.style.SUCCESS(f"Loaded {len(places):,} places from {path.name}."))
