import logging
import re
import time
from collections import defaultdict
from types import SimpleNamespace

import requests
from django.apps import apps
from django.utils import timezone
from simple_history.utils import bulk_update_with_history

import app
from app import cache_utils
from app.models import Item, Manga, MediaTypes, Sources, Status
from app.providers import mangabaka, services
from app.services import item_merge
from app.services.watch_state import PROJECTED_MEDIA_TYPES
from app.tasks_backfill_state import reset_backfill_state_for_identity_change
from integrations import import_progress
from integrations.imports import helpers, remote_removals
from integrations.imports.helpers import MediaImportError

logger = logging.getLogger(__name__)


def publish_in_place_update(user, model, objs, write):
    """Run ``write`` for rows updated in place and report them like new rows.

    bulk_update sends no save signal. This importer is day scoped
    (DAY_SCOPED_IMPORTERS), so History and Statistics refresh only the days it
    reports, and the watched-state projection only sees rows it is told about.
    The days a row sat on before the update are reported too, since a changed
    date moves the row off them.
    """
    columns = {field.name for field in model._meta.concrete_fields}
    date_fields = [name for name in ("start_date", "end_date") if name in columns]
    previous = []
    if date_fields:
        previous = [
            SimpleNamespace(**values)
            for values in model.objects.filter(
                pk__in=[obj.pk for obj in objs],
            ).values(*date_fields)
        ]
    helpers.retry_on_lock(write)
    helpers.note_bulk_rows([*objs, *previous])
    if model._meta.model_name in PROJECTED_MEDIA_TYPES:
        helpers.project_imported_watch_state(user, {obj.item_id for obj in objs})


SOURCE_LABEL = "MangaUpdates"

# MangaUpdates is the list; MangaBaka is the metadata. Every manga is stored
# under its MangaBaka series, and the MangaUpdates id rides along in
# provider_external_ids so the next run can find the row again.
MU_ID_KEY = "mangaupdates_id"
MANGABAKA_LOOKUP_URL = f"{mangabaka.base_url}/source/manga-updates/{{}}"
BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
NAMED_IN_MESSAGE = 5


def mu_short_id(mu_id):
    """Return MangaUpdates' 7-character short id ("0aoflwe") for a numeric id."""
    number = int(mu_id)
    text = ""
    while number:
        number, remainder = divmod(number, 36)
        text = BASE36_DIGITS[remainder] + text
    return text.zfill(7)


def mangabaka_lookup(mu_id):
    """Return the active MangaBaka series that record this MangaUpdates id."""
    try:
        response = services.api_request(
            Sources.MANGABAKA.value,
            "GET",
            MANGABAKA_LOOKUP_URL.format(mu_short_id(mu_id)),
            headers=mangabaka.HEADERS,
        )
    except requests.exceptions.HTTPError as error:
        if (
            error.response is not None
            and error.response.status_code == requests.codes.not_found
        ):
            return []
        raise
    series = (response.get("data") or {}).get("series") or []
    return [row for row in series if row.get("state") == "active"]


def _normalized(title):
    return re.sub(r"[\W_]+", "", (title or "").casefold())


def _mangabaka_titles(series):
    """Every title MangaBaka knows a series by."""
    titles = [series.get(key) for key in ("title", "romanized_title", "native_title")]
    for group in (series.get("secondary_titles") or {}).values():
        titles.extend(row.get("title") for row in group or [] if isinstance(row, dict))
    titles.extend(
        row.get("title") for row in series.get("titles") or [] if isinstance(row, dict)
    )
    return {_normalized(title) for title in titles if title}


def match_mangabaka(mu_id, mu_title):
    """Return ``(series, None)`` for a safe MangaBaka match, else ``(None, reason)``.

    Safe means exactly one active series records this MangaUpdates id and the
    MangaUpdates title is one of its titles. Anything else stays on
    MangaUpdates so a wrong series is never attached.
    """
    matches = mangabaka_lookup(mu_id)
    if not matches:
        return None, "not on MangaBaka"
    if len(matches) > 1:
        return None, "several MangaBaka series"
    series = matches[0]
    if _normalized(mu_title) not in _mangabaka_titles(series):
        return None, f"title differs on MangaBaka ({series.get('title')!r})"
    return series, None


def mangabaka_item_fields(series):
    """Return the Item columns MangaBaka's lookup response can fill."""
    title = series.get("title") or ""
    return {
        **Item.title_fields_from_metadata(
            {
                "title": title,
                "localized_title": title,
                "original_title": series.get("romanized_title"),
            },
        ),
        "image": mangabaka.get_image_url(series),
        "synopsis": series.get("description") or "",
        "source_url": series.get("canonical_url") or "",
        "genres": mangabaka.get_genres(series),
    }


def rekey_to_mangabaka(item, series, mu_id):
    """Move a MangaUpdates Item onto its MangaBaka series in place.

    Same approach as Floppy's TMDB -> TVDB migration: the Item keeps its
    primary key, so the Manga row, history, Date Added and lists stay put.
    Returns False, untouched, when the MangaBaka series is already tracked.
    """
    mb_id = str(series["id"])
    existing = Item.objects.filter(
        source=Sources.MANGABAKA.value,
        media_id=mb_id,
        media_type=MediaTypes.MANGA.value,
        library_media_type=item.library_media_type,
    ).first()
    if existing is not None:
        if Manga.objects.filter(item=existing).exists():
            return False
        # An untracked MangaBaka Item only blocks the unique constraint.
        item_merge.merge_item(loser=existing, keeper=item)

    item.source = Sources.MANGABAKA.value
    item.media_id = mb_id
    item.provider_external_ids = {
        **(item.provider_external_ids or {}),
        MU_ID_KEY: str(mu_id),
    }
    for field, value in mangabaka_item_fields(series).items():
        setattr(item, field, value)
    item.save()
    reset_backfill_state_for_identity_change([item])
    return True


def _list_titles(titles):
    """Return ``A, B, C and N more`` for a warning line."""
    shown = ", ".join(titles[:NAMED_IN_MESSAGE])
    rest = len(titles) - NAMED_IN_MESSAGE
    return f"{shown} and {rest} more" if rest > 0 else shown


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

        # MangaUpdates id -> the user's Manga row, whichever source it is
        # stored under now.
        manga = MediaTypes.MANGA.value
        self.existing_by_mu = dict(
            self.existing_media[manga][Sources.MANGAUPDATES.value]
        )
        mangabaka_rows = self.existing_media[manga][Sources.MANGABAKA.value]
        for mb_id, external_ids in Manga.objects.filter(
            user=user,
            item__source=Sources.MANGABAKA.value,
        ).values_list("item__media_id", "item__provider_external_ids"):
            mu_id = (external_ids or {}).get(MU_ID_KEY)
            if mu_id and mb_id in mangabaka_rows:
                self.existing_by_mu[str(mu_id)] = mangabaka_rows[mb_id]

        # Titles kept on MangaUpdates metadata because no safe MangaBaka match.
        self.stayed_on_mu = []
        self.converted_count = 0

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
        self._convert_leftovers()

        created_total = sum(len(media_list) for media_list in self.bulk_media.values())
        updated_total = sum(len(media_list) for media_list in self.bulk_update.values())

        if created_total > 0 or updated_total > 0 or self.converted_count > 0:
            cache_utils.clear_media_list_cache_for_user(self.user.id)
            cache_utils.clear_home_row_cache_for_user(self.user.id)

        if self.stayed_on_mu:
            self.warnings.append(
                f"{len(self.stayed_on_mu)} manga kept MangaUpdates metadata "
                f"(no safe MangaBaka match): {_list_titles(self.stayed_on_mu)}.",
            )

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

            publish_in_place_update(self.user, model, objs, do_update)
            logger.info(
                "Smart-diff updated %d %s row(s) in-place for user %s",
                len(objs),
                media_type,
                self.user.username,
            )

    def _safe_match(self, mu_id, title):
        """Return a safe MangaBaka series or None; MangaBaka being down is not fatal."""
        try:
            series, reason = match_mangabaka(mu_id, title)
        except Exception:
            logger.exception("MangaBaka lookup failed for MangaUpdates %s", mu_id)
            return None
        if series is None:
            logger.info("%s stays on MangaUpdates: %s", title, reason)
        return series

    def _mangabaka_item(self, mu_id, title):
        """Return the MangaBaka Item for a new MangaUpdates entry, or None."""
        series = self._safe_match(mu_id, title)
        if series is None:
            return None
        mb_id = str(series["id"])
        manga = MediaTypes.MANGA.value
        if mb_id in self.existing_media[manga][Sources.MANGABAKA.value]:
            # Already tracked under another MangaUpdates entry.
            logger.info("%s: MangaBaka %s is already tracked", title, mb_id)
            return None
        item, created = Item.objects.get_or_create(
            source=Sources.MANGABAKA.value,
            media_id=mb_id,
            media_type=manga,
            defaults={
                **mangabaka_item_fields(series),
                "provider_external_ids": {MU_ID_KEY: str(mu_id)},
            },
        )
        if not created and (item.provider_external_ids or {}).get(MU_ID_KEY) != str(
            mu_id
        ):
            item.provider_external_ids = {
                **(item.provider_external_ids or {}),
                MU_ID_KEY: str(mu_id),
            }
            item.save(update_fields=["provider_external_ids"])
        return item

    def _convert_leftovers(self):
        """Move rows still on MangaUpdates to MangaBaka once a safe match exists."""
        manga = MediaTypes.MANGA.value
        source = Sources.MANGAUPDATES.value
        for mu_id, row in self.existing_media[manga][source].items():
            if f"{manga}|{source}|{mu_id}" not in self.seen_remote:
                continue
            item = Item.objects.get(pk=row.item_id)
            title = item.title
            series = self._safe_match(mu_id, title)
            try:
                if series is not None and rekey_to_mangabaka(item, series, mu_id):
                    self.converted_count += 1
                    continue
            except Exception:
                logger.exception("Could not move %s to MangaBaka", title)
            self.stayed_on_mu.append(title)
        if self.converted_count:
            logger.info(
                "Moved %d manga to MangaBaka for user %s",
                self.converted_count,
                self.user.username,
            )

    def _report_removals(self, total_items):
        """Hand rows MangaUpdates no longer lists to the removal tracker."""
        media_type = MediaTypes.MANGA.value
        source = Sources.MANGAUPDATES.value
        # Keyed by MangaUpdates id whatever the row is stored under, so the
        # tracker's state carries over from before the MangaBaka move.
        candidates = {
            f"{media_type}|{source}|{mu_id}": media
            for mu_id, media in self.existing_by_mu.items()
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
        existing = self.existing_by_mu.get(media_id)

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

        item = self._mangabaka_item(media_id, title)
        if item is None:
            self.stayed_on_mu.append(title)
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
