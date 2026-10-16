import logging
import time
from collections import defaultdict

import requests
from django.apps import apps
from django.utils import timezone
from simple_history.utils import bulk_update_with_history

import app
from app import cache_utils
from app.models import MediaTypes, Sources, Status
from integrations import import_progress
from integrations.imports import helpers, remote_removals
from integrations.imports.helpers import MediaImportError

logger = logging.getLogger(__name__)

SOURCE_LABEL = "MangaUpdates"

# ==============================================================================
# MangaUpdates Configuration
# ==============================================================================
# Leave MANGAUPDATES_USERNAME as "" to use the username entered in the Floppy UI.
# Enter your MangaUpdates password below.
MANGAUPDATES_USERNAME = ""
MANGAUPDATES_PASSWORD = "<REDACTED_MANGAUPDATES_PASSWORD>"

BASE_URL = "https://api.mangaupdates.com/v1"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "Floppy-Media-Tracker/1.0",
}

LIST_TYPE_TO_STATUS = {
    "read": Status.IN_PROGRESS.value,
    "complete": Status.COMPLETED.value,
    "unfinished": Status.DROPPED.value,
    "hold": Status.PAUSED.value,
    "wish": Status.PLANNING.value,
}


def get_token(request, redirect_uri=None):
    """Fallback stub for OAuth token endpoint."""
    return {"access_token": "", "username": ""}


def importer(token, user, mode, username):
    """Import manga tracking data from MangaUpdates."""
    effective_username = MANGAUPDATES_USERNAME.strip() or (username or "").strip()
    password = MANGAUPDATES_PASSWORD.strip()

    if not effective_username or not password:
        msg = (
            "MangaUpdates username or password missing. "
            "Please ensure MANGAUPDATES_PASSWORD is set in anilist.py."
        )
        raise MediaImportError(msg)

    mangaupdates_importer = MangaUpdatesImporter(
        user, mode, effective_username, password
    )
    return mangaupdates_importer.import_data()


class MangaUpdatesImporter:
    """Mirror a user's MangaUpdates lists into Floppy.

    MangaUpdates is the ground truth. Existing rows are updated in place so
    Date Added survives; rows it stopped listing are reported through
    ``remote_removals`` rather than deleted.
    """

    def __init__(self, user, mode, username, password):
        """Load the user's existing manga so the run can diff against it."""
        self.user = user
        self.mode = mode
        self.username = username
        self.password = password
        self.session_token = None
        self.warnings = []

        # Load existing tracked media for smart diff
        self.existing_media = helpers.get_existing_media(user)
        self.to_delete = defaultdict(lambda: defaultdict(set))
        self.bulk_media = defaultdict(list)
        self.bulk_update = defaultdict(list)
        self.skipped_count = 0

        # Every "manga|mangaupdates|id" the remote listed this run.
        self.seen_remote = set()

    def _login(self):
        """Authenticate with MangaUpdates API to acquire a session token."""
        url = f"{BASE_URL}/account/login"
        payload = {"username": self.username, "password": self.password}
        try:
            resp = requests.put(url, json=payload, headers=HEADERS, timeout=15)
            if resp.status_code == requests.codes.unauthorized:
                msg = f"Invalid credentials for MangaUpdates user '{self.username}'."
                raise MediaImportError(msg)
            resp.raise_for_status()
            data = resp.json()
            token = data.get("context", {}).get("session_token")
            if not token:
                msg = "MangaUpdates login did not return a session_token."
                raise MediaImportError(msg)
            self.session_token = token
            logger.info(
                "Successfully authenticated with MangaUpdates as %s", self.username
            )
        except requests.exceptions.RequestException as exc:
            msg = f"Failed to connect to MangaUpdates login API: {exc}"
            raise MediaImportError(msg) from exc

    def _get_auth_headers(self):
        return {
            **HEADERS,
            "Authorization": f"Bearer {self.session_token}",
        }

    def _api_post_with_retry(self, url, json_payload, max_retries=3):
        """POST request with rate-limit and transient error handling."""
        auth_headers = self._get_auth_headers()
        for attempt in range(max_retries):
            try:
                resp = requests.post(
                    url, json=json_payload, headers=auth_headers, timeout=25
                )
                if resp.status_code == requests.codes.too_many_requests:
                    wait_time = 2 ** (attempt + 1)
                    logger.warning(
                        "MangaUpdates rate-limited (429). Waiting %ss...", wait_time
                    )
                    time.sleep(wait_time)
                    continue
                if resp.status_code in (502, 503, 504):
                    wait_time = 2**attempt
                    logger.warning(
                        "MangaUpdates server error (%s). Retrying in %ss...",
                        resp.status_code,
                        wait_time,
                    )
                    time.sleep(wait_time)
                    continue
                if resp.status_code == requests.codes.unauthorized:
                    logger.info("Session token expired. Re-authenticating...")
                    self._login()
                    auth_headers = self._get_auth_headers()
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException:
                if attempt == max_retries - 1:
                    raise
                time.sleep(1)
        msg = f"Failed to fetch data from {url} after {max_retries} retries."
        raise MediaImportError(msg)

    def import_data(self):
        """Import all reading lists from MangaUpdates."""
        self._login()

        # 1. Fetch user lists (Reading, Wish, Complete, Unfinished, On Hold, etc.)
        lists_url = f"{BASE_URL}/lists"
        try:
            resp = requests.get(lists_url, headers=self._get_auth_headers(), timeout=15)
            resp.raise_for_status()
            user_lists = resp.json()
        except requests.exceptions.RequestException as exc:
            msg = f"Failed to fetch MangaUpdates user lists: {exc}"
            raise MediaImportError(msg) from exc

        if not user_lists:
            logger.warning("No lists found for MangaUpdates user %s", self.username)
            return {MediaTypes.MANGA.value: 0}, "No reading lists found."

        # Collect total entries across all lists for progress reporting
        total_items = 0
        all_entries_by_list = []

        for ulist in user_lists:
            list_id = ulist.get("list_id")
            list_title = ulist.get("title") or ulist.get("type")
            list_type = ulist.get("type", "read")
            search_url = f"{BASE_URL}/lists/{list_id}/search"

            page = 1
            perpage = 100
            list_entries = []

            while True:
                data = self._api_post_with_retry(
                    search_url, {"page": page, "perpage": perpage}
                )
                results = data.get("results") or []
                total_hits = data.get("total_hits") or 0

                list_entries.extend((r, list_type) for r in results)

                if not results or (page * perpage) >= total_hits:
                    break

                page += 1
                time.sleep(0.2)  # Polite request spacing

            total_items += len(list_entries)
            all_entries_by_list.append((list_title, list_entries))
            logger.info(
                "Found %d manga in MangaUpdates list '%s'",
                len(list_entries),
                list_title,
            )

        self._progress_total = total_items
        self._progress_current = 0

        # 2. Process all entries
        for _list_title, entries in all_entries_by_list:
            for entry_data, list_type in entries:
                self._progress_current += 1
                import_progress.report(
                    self._progress_current,
                    self._progress_total,
                    "MangaUpdates",
                )
                try:
                    self._process_entry(entry_data, list_type)
                except Exception as exc:
                    logger.warning("Error processing MangaUpdates entry: %s", exc)
                    self.warnings.append(f"Error processing entry: {exc}")

        # 3. Apply cleanup, bulk create new, and in-place update existing
        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)
        self._apply_bulk_updates()

        created_total = sum(len(media_list) for media_list in self.bulk_media.values())
        updated_total = sum(len(media_list) for media_list in self.bulk_update.values())

        if created_total > 0 or updated_total > 0:
            cache_utils.clear_media_list_cache_for_user(self.user.id)
            cache_utils.clear_home_row_cache_for_user(self.user.id)

        self._report_removals(total_items)

        imported_counts = defaultdict(int)
        for media_type, media_list in self.bulk_media.items():
            imported_counts[media_type] += len(media_list)
        for media_type, media_list in self.bulk_update.items():
            imported_counts[media_type] += len(media_list)

        if MediaTypes.MANGA.value not in imported_counts:
            imported_counts[MediaTypes.MANGA.value] = 0
        if created_total:
            imported_counts["created"] = created_total
        if updated_total:
            imported_counts["updated"] = updated_total
        if self.skipped_count:
            imported_counts["skipped"] = self.skipped_count

        deduplicated_messages = "\n".join(dict.fromkeys(self.warnings))
        return dict(imported_counts), deduplicated_messages

    def _apply_bulk_updates(self):
        """Persist in-place updates with history without altering created_at (Date Added)."""
        fields = [
            "status",
            "score",
            "scored_at",
            "progress",
            "progressed_at",
            "start_date",
            "end_date",
        ]
        for media_type, objs in self.bulk_update.items():
            if not objs:
                continue
            model = apps.get_model(app_label="app", model_name=media_type)

            def do_update(objs=objs, model=model, fields=fields):
                return bulk_update_with_history(
                    objs,
                    model,
                    fields=fields,
                    batch_size=500,
                    default_user=self.user,
                    default_date=timezone.now(),
                    default_change_reason="MangaUpdates import",
                )

            helpers.retry_on_lock(do_update)
            logger.info(
                "Smart-diff updated %d %s row(s) in-place for user %s",
                len(objs),
                media_type,
                self.user.username,
            )

    def _report_removals(self, total_items):
        """Hand rows MangaUpdates no longer lists to the removal tracker."""
        media_type = MediaTypes.MANGA.value
        source = Sources.MANGAUPDATES.value
        candidates = {
            f"{media_type}|{source}|{media_id}": media
            for media_id, media in self.existing_media[media_type][source].items()
        }
        try:
            warning = remote_removals.sync(
                self.user,
                SOURCE_LABEL,
                {"manga": total_items},
                self.seen_remote,
                candidates,
            )
        except Exception:
            logger.exception(
                "MangaUpdates removal check failed for user %s", self.user.username
            )
            warning = "Removal check failed this run; see the server log."
        if warning:
            self.warnings.append(warning)

    def _process_entry(self, entry_data, list_type):
        """Process a single MangaUpdates entry with in-place SMART DIFF comparison."""
        record = entry_data.get("record") or {}
        metadata = entry_data.get("metadata") or {}
        series_record = record.get("series") or {}

        series_id = series_record.get("id")
        if not series_id:
            return

        media_id = str(series_id)
        source = Sources.MANGAUPDATES.value
        media_type = MediaTypes.MANGA.value
        title = series_record.get("title") or "Unknown Manga"
        self.seen_remote.add(f"{media_type}|{source}|{media_id}")

        # Reading progress & status
        status_info = record.get("status") or {}
        progress = status_info.get("chapter") or 0
        raw_score = metadata.get("user_rating")
        score = float(raw_score) if raw_score is not None else None
        status = LIST_TYPE_TO_STATUS.get(list_type, Status.IN_PROGRESS.value)

        # ======================================================================
        # SMART DIFF CHECK:
        # Check existing manga row in Floppy's database
        # ======================================================================
        existing = self.existing_media[media_type][source].get(media_id)

        if existing is not None:
            existing_progress = existing.progress or 0
            existing_status = existing.status
            existing_score = (
                float(existing.score) if existing.score is not None else None
            )

            progress_matches = existing_progress == progress
            status_matches = existing_status == status
            score_matches = existing_score == score

            # If nothing changed, SKIP completely!
            if progress_matches and status_matches and score_matches:
                self.skipped_count += 1
                return

            # Something changed (e.g. read chapter 98 -> 99, or status/score changed)
            # Update IN-PLACE to preserve created_at (Date Added)!
            now_dt = timezone.now()
            logger.info(
                "Smart-diff updating %s in-place (ch: %s -> %s, status: %s -> %s)",
                title,
                existing_progress,
                progress,
                existing_status,
                status,
            )
            if not score_matches:
                existing.score = score
                existing.scored_at = now_dt if score is not None else None
            if not progress_matches:
                existing.progress = progress
                existing.progressed_at = now_dt
                existing.end_date = now_dt
                if existing.start_date is None and progress > 0:
                    existing.start_date = now_dt
            if not status_matches:
                existing.status = status
                if status == Status.COMPLETED.value and existing.end_date is None:
                    existing.end_date = now_dt
                if status != Status.PLANNING.value and existing.start_date is None:
                    existing.start_date = now_dt
            existing._history_date = now_dt
            self.bulk_update[media_type].append(existing)
            return

        # 4. Catalog Item get_or_create (Brand new manga)
        series_meta = metadata.get("series") or {}
        image_url = ""
        img_dict = series_meta.get("image") or {}
        if isinstance(img_dict, dict):
            url_dict = img_dict.get("url") or {}
            image_url = url_dict.get("original") or url_dict.get("thumb") or ""

        genres_list = [
            g["genre"]
            for g in (series_meta.get("genres") or [])
            if isinstance(g, dict) and "genre" in g
        ]
        synopsis = series_meta.get("description") or ""

        item, _ = app.models.Item.objects.get_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            defaults={
                **app.models.Item.title_fields_from_metadata(
                    {
                        "title": title,
                        "localized_title": title,
                        "original_title": None,
                    }
                ),
                "image": image_url,
                "genres": genres_list,
                "synopsis": synopsis,
            },
        )

        # 5. Build user tracking model instance
        now_dt = timezone.now()
        is_started = status != Status.PLANNING.value or progress > 0
        model = apps.get_model(app_label="app", model_name=media_type)
        instance = model(
            item=item,
            user=self.user,
            score=score,
            progress=progress,
            status=status,
            start_date=now_dt if is_started else None,
            end_date=now_dt if is_started else None,
            notes="",
        )
        instance._history_date = now_dt
        self.bulk_media[media_type].append(instance)
