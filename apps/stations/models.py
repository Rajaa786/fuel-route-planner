from django.db import models


class Place(models.Model):
    """One gazetteer lookup key: a normalised place name within a US state.

    Rows come from ``data/us_places.csv.gz`` (derived from GeoNames). Name
    collisions inside a state are resolved when that file is built, so
    ``(key, state)`` is unique and a lookup is a single index hit.
    """

    key = models.CharField(max_length=80, help_text="Output of normalize_place_name().")
    state = models.CharField(max_length=2)
    name = models.CharField(max_length=120)
    latitude = models.FloatField()
    longitude = models.FloatField()
    population = models.PositiveIntegerField(default=0)
    ambiguous = models.BooleanField(
        default=False,
        help_text=(
            "Another same-name place exists elsewhere in the state and population cannot "
            "tell them apart. Fine as a best guess for a trip endpoint; never used to "
            "position a fuel station."
        ),
    )

    class Meta:
        constraints = [models.UniqueConstraint(fields=["key", "state"], name="unique_place_key")]

    def __str__(self) -> str:
        return f"{self.name}, {self.state}"


class FuelStation(models.Model):
    """A truck stop from the fuel-price file, geocoded to its city centroid."""

    opis_id = models.PositiveIntegerField(unique=True, help_text="OPIS Truckstop ID.")
    name = models.CharField(max_length=120)
    address = models.CharField(max_length=200)
    city = models.CharField(max_length=80)
    state = models.CharField(max_length=2)
    rack_id = models.PositiveIntegerField(null=True, blank=True)
    retail_price = models.DecimalField(
        max_digits=6, decimal_places=3, help_text="US dollars per gallon."
    )
    latitude = models.FloatField()
    longitude = models.FloatField()

    class Meta:
        ordering = ["opis_id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(retail_price__gt=0), name="fuel_station_price_positive"
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.city}, {self.state})"
