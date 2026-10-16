"""Run a mirror import against the real database and roll it back.

Shows what the SIMKL or MangaUpdates importer would create, update, skip and
flag for a user, using the credentials of that user's scheduled import, and
then rolls the transaction back. Provider calls (TMDB, MAL, SIMKL,
MangaUpdates) are real and read-only; nothing is written to the database, the
removal tracker's state, or the review list.

Usage, inside the container:

    python manage.py mirror_dry_run simkl --username <floppy user>
    python manage.py mirror_dry_run mangaupdates --username <floppy user>
"""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_celery_beat.models import PeriodicTask

from app.mixins import disable_fetch_releases
from app.providers import credentials
from integrations.imports import anilist, remote_removals, simkl

SOURCES = {
    "simkl": ("Import from SIMKL", simkl.importer),
    "mangaupdates": ("Import from AniList", anilist.importer),
}


class Command(BaseCommand):
    """Dry-run a SIMKL or MangaUpdates mirror import for one user."""

    help = "Run a mirror import inside a transaction and roll it back"

    def add_arguments(self, parser):
        """Add command line arguments."""
        parser.add_argument("source", choices=sorted(SOURCES))
        parser.add_argument("--username", required=True, help="Floppy username")

    def handle(self, *_args, **options):
        """Execute the command."""
        task_name, importer = SOURCES[options["source"]]
        user = get_user_model().objects.filter(username=options["username"]).first()
        if user is None:
            msg = f"No user named {options['username']!r}."
            raise CommandError(msg)

        kwargs = None
        for task in PeriodicTask.objects.filter(task=task_name):
            candidate = json.loads(task.kwargs or "{}")
            if candidate.get("user_id") == user.id:
                kwargs = candidate
                break
        if kwargs is None:
            msg = f"{user.username} has no scheduled '{task_name}' task to borrow credentials from."
            raise CommandError(msg)

        previews = []

        def preview(*args):
            skip_message, missing = remote_removals.evaluate(*args)
            previews.append((args[1], args[2], skip_message, missing, args[4]))
            return skip_message or ""

        with (
            transaction.atomic(),
            disable_fetch_releases(),
            credentials.current_user_scope(user),
            patch.object(remote_removals, "sync", preview),
            patch("app.backfill_queue.reschedule"),
        ):
            if options["source"] == "simkl":
                counts, warnings = importer(
                    kwargs["token"],
                    user,
                    kwargs.get("mode", "overwrite"),
                    refresh_token=kwargs.get("refresh_token"),
                )
            else:
                counts, warnings = importer(
                    kwargs.get("token"),
                    user,
                    kwargs.get("mode", "overwrite"),
                    kwargs.get("username"),
                )
            self._report(counts, warnings, previews)
            transaction.set_rollback(True)

        self.stdout.write(self.style.SUCCESS("Rolled back. Nothing was written."))

    def _report(self, counts, warnings, previews):
        self.stdout.write(f"Counts: {dict(counts)}")
        self.stdout.write(f"Message: {warnings or '(none)'}")
        for label, remote_counts, skip_message, missing, candidates in previews:
            self.stdout.write(f"{label} remote counts: {remote_counts}")
            if skip_message:
                self.stdout.write(f"  {skip_message}")
                continue
            if not missing:
                self.stdout.write("  Every local row is still on the remote.")
                continue
            self.stdout.write(
                f"  {len(missing)} local row(s) not on {label} "
                f"(reported after {remote_removals.CONFIRM_RUNS} runs):"
            )
            for row_key, runs in sorted(missing.items()):
                media = candidates[row_key]
                self.stdout.write(f"    [{runs} run(s)] {media.item.title} ({row_key})")
