"""Re-key a user's MangaUpdates manga onto their MangaBaka series, in place.

Each MangaUpdates manga is looked up on MangaBaka by its MangaUpdates id. A
safe match (one active series, and the MangaUpdates title is one of its
titles) re-keys the Item in place through ``anilist.rekey_to_mangabaka``, the
same way Floppy's own TMDB -> TVDB migration does it. The Manga row, history,
Date Added and lists keep pointing at the same Item.

Anything without a safe match stays on MangaUpdates and is listed.

All lookups run first, with no database transaction open, so the SQLite file is
never locked while MangaBaka's 30-a-minute limit paces the run. The writes then
happen in one short transaction.

Usage, inside the container:

    python manage.py mangabaka_convert --username ayham --limit 20 --dry-run
    python manage.py mangabaka_convert --username ayham --all
    python manage.py mangabaka_convert --username ayham --pks 6962 1234
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from app import cache_utils, history_cache, statistics_cache
from app.models import Manga, Sources
from integrations.imports import anilist


class Command(BaseCommand):
    """Re-key MangaUpdates manga onto MangaBaka in place."""

    help = "Move a user's MangaUpdates manga onto MangaBaka without recreating rows"

    def add_arguments(self, parser):
        """Add command line arguments."""
        parser.add_argument("--username", required=True, help="Floppy username")
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--limit", type=int, help="Convert the first N rows by id")
        group.add_argument("--pks", type=int, nargs="+", help="Convert these Manga ids")
        group.add_argument("--all", action="store_true", help="Convert every row")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Do every lookup and write, then roll back",
        )

    def handle(self, *_args, **options):
        """Execute the command."""
        user = get_user_model().objects.filter(username=options["username"]).first()
        if user is None:
            msg = f"No user named {options['username']!r}."
            raise CommandError(msg)

        rows = (
            Manga.objects.filter(user=user, item__source=Sources.MANGAUPDATES.value)
            .select_related("item")
            .order_by("pk")
        )
        if options["pks"]:
            rows = rows.filter(pk__in=options["pks"])
        elif options["limit"]:
            rows = rows[: options["limit"]]
        rows = list(rows)

        results = {"converted": [], "held": [], "conflict": [], "failed": []}
        planned = []
        for done, row in enumerate(rows, start=1):
            label = f"#{row.pk} {row.item.title} (MU {row.item.media_id})"
            try:
                series, reason = anilist.match_mangabaka(
                    row.item.media_id, row.item.title
                )
            except Exception as error:
                results["failed"].append(f"{label}: {error}")
                continue
            if series is None:
                results["held"].append(f"{label}: {reason}")
            else:
                planned.append((row, series, label))
            if done % 50 == 0:
                self.stdout.write(f"looked up {done}/{len(rows)}")
                self.stdout.flush()

        with transaction.atomic():
            for row, series, label in planned:
                if anilist.rekey_to_mangabaka(row.item, series, row.item.media_id):
                    results["converted"].append(
                        f"{label} -> MangaBaka {series['id']} "
                        f"{series.get('title')!r} ({series.get('type')})",
                    )
                else:
                    results["conflict"].append(
                        f"{label} -> MangaBaka {series['id']} is already tracked",
                    )
            if options["dry_run"]:
                transaction.set_rollback(True)
            else:
                transaction.on_commit(lambda: self._invalidate(user.id))

        self._report(results, dry_run=options["dry_run"])

    def _invalidate(self, user_id):
        """Clear the caches that hold titles, covers and sources."""
        cache_utils.clear_media_list_cache_for_user(user_id)
        cache_utils.clear_home_row_cache_for_user(user_id)
        history_cache.invalidate_history_cache(user_id, force=True)
        statistics_cache.invalidate_all_statistics_days(
            user_id,
            reason="mangabaka_convert",
        )

    def _report(self, results, *, dry_run):
        """Print the outcome."""
        for key, lines in results.items():
            self.stdout.write(f"{key}: {len(lines)}")
            for line in lines:
                self.stdout.write(f"  {line}")
        if dry_run:
            self.stdout.write("Rolled back. Nothing was written.")
