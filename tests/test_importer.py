from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from apps.stations.importer import clean_and_geocode
from apps.stations.index import get_station_index
from apps.stations.models import FuelStation, Place

PLACES = {("BIGCABIN", "OK"): (36.53787, -95.22136), ("TOMAH", "WI"): (43.97858, -90.50402)}


def row(opis_id, city, state, price, name="WOODSHED", address="I-44, EXIT 283", rack="307"):
    return {
        "OPIS Truckstop ID": str(opis_id),
        "Truckstop Name": name,
        "Address": address,
        "City": city,
        "State": state,
        "Rack ID": rack,
        "Retail Price": price,
    }


def test_strips_whitespace_and_geocodes_by_city():
    report = clean_and_geocode([row(7, "Big Cabin        ", "OK", "3.00733333")], PLACES)
    [station] = report.stations
    assert (station.city, station.state) == ("Big Cabin", "OK")
    assert (station.latitude, station.longitude) == PLACES[("BIGCABIN", "OK")]
    assert station.retail_price == Decimal("3.007")
    assert station.rack_id == 307


def test_duplicate_opis_ids_collapse_to_the_lowest_quote():
    rows = [
        row(7, "Big Cabin", "OK", "3.269"),
        row(7, "Big Cabin", "OK", "3.199"),
        row(7, "Big Cabin", "OK", "3.429"),
    ]
    report = clean_and_geocode(rows, PLACES)
    assert [s.retail_price for s in report.stations] == [Decimal("3.199")]
    assert report.duplicate_rows_merged == 2


def test_rows_outside_the_usa_are_dropped():
    report = clean_and_geocode(
        [row(1, "Toronto", "ON", "4.5"), row(9, "Tomah", "WI", "3.287")], PLACES
    )
    assert [s.opis_id for s in report.stations] == [9]
    assert report.rows_outside_usa == 1


@pytest.mark.parametrize("price", ["", "abc", "0", "-1", "NaN", "Infinity", "0.0004", "1e4"])
def test_invalid_prices_are_rejected(price):
    report = clean_and_geocode([row(7, "Big Cabin", "OK", price)], PLACES)
    assert report.stations == []
    assert report.rows_invalid == 1


def test_malformed_rows_are_counted_not_fatal():
    short_line = {"OPIS Truckstop ID": "7", "Truckstop Name": "WOODSHED", "Address": None,
                  "City": None, "State": "OK", "Rack ID": None, "Retail Price": None}  # fmt: skip
    rows = [
        short_line,
        row(-7, "Big Cabin", "OK", "3.1"),
        row("x7", "Big Cabin", "OK", "3.1"),
        {"State": None},
        row(9, "Tomah", "WI", "3.287"),
    ]
    report = clean_and_geocode(rows, PLACES)

    assert [s.opis_id for s in report.stations] == [9]
    assert (report.rows_read, report.rows_invalid, report.rows_outside_usa) == (5, 3, 1)


def test_station_in_unknown_city_is_reported_not_guessed():
    report = clean_and_geocode([row(7, "Nowhereville", "OK", "3.0")], PLACES)
    assert report.stations == []
    assert report.stations_not_geocoded == ["7: Nowhereville, OK"]


# --- management command --------------------------------------------------------------------

CSV_HEADER = "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"


@pytest.fixture
def gazetteer(db):
    Place.objects.bulk_create(
        [
            Place(key="BIGCABIN", state="OK", name="Big Cabin", latitude=36.5, longitude=-95.2),
            Place(key="TOMAH", state="WI", name="Tomah", latitude=43.9, longitude=-90.5),
            Place(
                key="GREENWOOD",
                state="LA",
                name="Greenwood",
                latitude=32.4,
                longitude=-93.9,
                ambiguous=True,
            ),
        ]
    )


def run_import(tmp_path, body):
    path = tmp_path / "fuel.csv"
    path.write_text(CSV_HEADER + body, encoding="utf-8")
    out = StringIO()
    call_command("import_fuel_stations", path=path, stdout=out)
    return out.getvalue()


def test_import_command_upserts_prunes_and_refreshes_the_index(gazetteer, tmp_path):
    run_import(
        tmp_path,
        '7,WOODSHED,"I-44, EXIT 283",Big Cabin,OK,307,3.10\n9,KWIK TRIP,"I-94",Tomah,WI,420,3.28\n',
    )
    assert FuelStation.objects.count() == 2
    assert len(get_station_index()) == 2

    # Re-import with a new price for #7 and without #9: update in place, prune the rest.
    output = run_import(tmp_path, '7,WOODSHED,"I-44, EXIT 283",Big Cabin,OK,307,2.95\n')
    assert list(FuelStation.objects.values_list("opis_id", "retail_price")) == [
        (7, Decimal("2.950"))
    ]
    assert "removed stale stations: 1" in output
    assert len(get_station_index()) == 1  # the in-memory index was invalidated


def test_import_command_never_places_a_station_in_an_ambiguous_city(gazetteer, tmp_path):
    output = run_import(
        tmp_path,
        '7,WOODSHED,"I-44",Big Cabin,OK,307,3.10\n8,LOVES,"I-20, EXIT 5",Greenwood,LA,1,2.50\n',
    )
    assert list(FuelStation.objects.values_list("opis_id", flat=True)) == [7]
    assert "Skipped 1 station(s)" in output


def test_import_command_refuses_to_run_without_a_gazetteer(db, tmp_path):
    with pytest.raises(CommandError, match="load_places"):
        run_import(tmp_path, "7,WOODSHED,I-44,Big Cabin,OK,307,3.10\n")


def test_import_command_refuses_to_empty_the_table(gazetteer, tmp_path):
    run_import(tmp_path, "7,WOODSHED,I-44,Big Cabin,OK,307,3.10\n")
    with pytest.raises(CommandError, match="refusing to empty"):
        run_import(tmp_path, "1,PETRO,HWY 401,Toronto,ON,1,4.50\n")
    assert FuelStation.objects.count() == 1


def test_shipped_gazetteer_loads_and_resolves_known_cities(db, settings):
    call_command("load_places", stdout=StringIO())
    assert Place.objects.count() > 150_000
    new_york = Place.objects.get(key="NEWYORK", state="NY")
    assert new_york.name == "New York City"
    assert (round(new_york.latitude, 1), round(new_york.longitude, 1)) == (40.7, -74.0)
