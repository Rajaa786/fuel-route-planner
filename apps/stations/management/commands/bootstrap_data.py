from django.core.management import call_command
from django.core.management.base import BaseCommand

from apps.stations.models import Place


class Command(BaseCommand):
    help = "One-shot, idempotent data setup: load the gazetteer, then import fuel stations."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force", action="store_true", help="Reload the gazetteer even if already loaded."
        )

    def handle(self, *args, force: bool, **options):
        if force or not Place.objects.exists():
            call_command("load_places", stdout=self.stdout)
        else:
            self.stdout.write("Gazetteer already loaded (use --force to reload).")
        call_command("import_fuel_stations", stdout=self.stdout)
