import logging
import time
from collections import defaultdict

import requests
from django.apps import apps
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from simple_history.utils import bulk_update_with_history

import app
from app import cache_utils
from app import helpers as app_helpers
from app.models import MediaTypes, Sources, Status
from app.providers import credentials, services
from integrations import import_progress
from integrations.imports import helpers, remote_removals
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError

logger = logging.getLogger(__name__)

SOURCE_LABEL = "SIMKL"

# Rows whose Date Added follows SIMKL's added_to_watchlist_at.
DATE_ADDED_TYPES = (
    MediaTypes.TV.value,
    MediaTypes.SEASON.value,
    MediaTypes.MOVIE.value,
    MediaTypes.ANIME.value,
)


def get_token(request, redirect_uri=None, code_verifier=None):
    """View for getting the SIMKL OAuth2 token.

    AUTH V2 sends a PKCE verifier; AUTH V1 apps have none and no refresh token.
    """
    data = {
        "code": request.GET["code"],
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri
        or app_helpers.build_absolute_app_url(
            request,
            reverse("import_simkl_private"),
        ),
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
    token_response = _request_token(data, auth_v1=not code_verifier)

    return {
        "access_token": token_response["access_token"],
        "refresh_token": token_response.get("refresh_token"),
        "username": get_username(token_response["access_token"]),
    }


def get_access_token(encrypted_refresh_token):
    """Get access token from encrypted refresh token.

    SIMKL refresh tokens don't rotate, so the stored one stays valid.
    """
    return _request_token(
        {
            "grant_type": "refresh_token",
            "refresh_token": helpers.decrypt_or_raise(encrypted_refresh_token),
        },
    )["access_token"]


def _request_token(data, auth_v1=False):
    """Exchange a code or refresh token at SIMKL's token endpoint."""
    headers = {
        "simkl-api-key": credentials.get("simkl", "client_id"),
        "User-Agent": f"Floppy/{settings.VERSION}",
    }
    data = {
        "client_id": credentials.get("simkl", "client_id"),
        "client_secret": credentials.get("simkl", "client_secret"),
        **data,
    }

    try:
        if auth_v1:
            return app.providers.services.api_request(
                "SIMKL",
                "POST",
                "https://api.simkl.com/oauth/token",
                headers=headers,
                params=data,
            )
        return app.providers.services.api_request(
            "SIMKL",
            "POST",
            "https://api.simkl.com/oauth2/token",
            headers=headers,
            data=data,
        )
    except requests.exceptions.HTTPError as error:
        if error.response.status_code in (
            requests.codes.unauthorized,
            requests.codes.forbidden,
        ):
            msg = (
                "SIMKL rejected the Client ID and Client secret. Check them in "
                "Settings > Metadata, and that the SIMKL app's redirect URI "
                "matches this Floppy address."
            )
            raise MediaImportError(msg) from error
        raise


def _api_headers(token):
    """Headers SIMKL requires on authenticated API calls."""
    return {
        "Authorization": f"Bearer {token}",
        "simkl-api-key": credentials.get("simkl", "client_id"),
        "User-Agent": f"Floppy/{settings.VERSION}",
    }


def _api_params():
    """Query parameters SIMKL requires on every API call."""
    return {
        "client_id": credentials.get("simkl", "client_id"),
        "app-name": "floppy",
        "app-version": settings.VERSION,
    }


def get_username(token):
    """Get the username from SIMKL using the provided token."""
    try:
        user_info = app.providers.services.api_request(
            "SIMKL",
            "GET",
            "https://api.simkl.com/users/settings",
            headers=_api_headers(token),
            params=_api_params(),
        )
    except services.ProviderAPIError as error:
        if error.status_code == requests.codes.unauthorized:
            msg = "Invalid SIMKL secret key."
            raise MediaImportError(msg) from error
        raise

    return user_info["user"]["name"]


def importer(
    token,
    user,
    mode,
    anime_destination=MediaTypes.ANIME.value,
    refresh_token=None,
):
    """Import tv shows, movies and anime from SIMKL with SMART-DIFF reconciliation."""
    simkl_importer = SimklImporter(token, user, mode, refresh_token=refresh_token)
    return simkl_importer.import_data()


class SimklImporter:
    """Mirror a user's SIMKL library into Floppy.

    SIMKL is the ground truth. Existing rows are updated in place so Date
    Added and history survive; rows SIMKL stopped listing are reported through
    ``remote_removals`` rather than deleted.
    """

    SIMKL_API_BASE_URL = "https://api.simkl.com"

    def __init__(
        self,
        token,
        user,
        mode,
        anime_destination=MediaTypes.ANIME.value,
        refresh_token=None,
    ):
        """Resolve the access token and load the user's current library."""
        if refresh_token:
            self.token = get_access_token(refresh_token)
        else:
            self.token = helpers.decrypt_or_raise(token)
        self.user = user
        self.mode = mode
        self.warnings = []

        # Load existing tracked media for smart-diff
        self.existing_media = helpers.get_existing_media(user)
        self.existing_children = helpers.get_existing_children(user)

        # Track media IDs to delete (unused for in-place updates, kept for compatibility)
        self.to_delete = defaultdict(lambda: defaultdict(set))

        # Track bulk creation lists for brand-new media
        self.bulk_media = defaultdict(list)

        # Track in-place bulk updates for existing media (preserves created_at / Date Added)
        self.bulk_update = defaultdict(list)
        self.skipped_count = 0

        # Every "type|source|id" SIMKL listed this run, for the removal check.
        self.seen_remote = set()

        # Existing rows whose Date Added must follow SIMKL. Written without a
        # history row: a date correction is not a change the user made.
        self.date_added_updates = defaultdict(list)

        logger.info(
            "Initialized Smart-Diff Simkl importer for user %s (mode: %s)",
            user.username,
            mode,
        )

    def _get_user_list(self, max_retries=3):
        """Fetch full user list with retry & backoff against transient 429/502/503 drops."""
        url = f"{self.SIMKL_API_BASE_URL}/sync/all-items/"
        params = {
            **_api_params(),
            "extended": "full",
            "episode_watched_at": "yes",
            "memos": "yes",
        }

        for attempt in range(max_retries):
            try:
                return app.providers.services.api_request(
                    "SIMKL",
                    "GET",
                    url,
                    headers=_api_headers(self.token),
                    params=params,
                )
            except services.ProviderAPIError as error:
                status_code = getattr(error, "status_code", None)
                if status_code in (429, 502, 503, 504) and attempt < max_retries - 1:
                    wait_sec = 2 ** (attempt + 1)
                    logger.warning(
                        "SIMKL API transient error %s on attempt %d/%d. Waiting %ss...",
                        status_code,
                        attempt + 1,
                        max_retries,
                        wait_sec,
                    )
                    time.sleep(wait_sec)
                    continue
                raise
        return None

    def import_data(self):
        """Import all user data from Simkl using Smart-Diff."""
        data = self._get_user_list()

        # Safety Guard (Stale Snapshot / Empty Data):
        # Never wipe or touch local library if the API returns empty/invalid data.
        if not data or not isinstance(data, dict):
            logger.warning("SIMKL returned empty or invalid data payload. Aborting.")
            return {}, "SIMKL returned no data."

        self._process_media_lists(data)

        # 1. Cleanup any explicitly marked rows (if any)
        helpers.cleanup_existing_media(self.to_delete, self.user)

        # 2. Bulk create brand-new media rows
        helpers.bulk_create_media(self.bulk_media, self.user)
        date_synced = self._apply_date_added()

        # 3. Apply in-place updates for modified existing media (preserving Date Added / created_at)
        self._apply_bulk_updates()

        created_total = sum(len(media_list) for media_list in self.bulk_media.values())
        updated_total = sum(len(media_list) for media_list in self.bulk_update.values())

        if created_total > 0 or updated_total > 0 or date_synced > 0:
            cache_utils.clear_media_list_cache_for_user(self.user.id)
            cache_utils.clear_home_row_cache_for_user(self.user.id)

        self._report_removals(data)

        imported_counts = defaultdict(int)
        for media_type, media_list in self.bulk_media.items():
            imported_counts[media_type] += len(media_list)
        for media_type, media_list in self.bulk_update.items():
            imported_counts[media_type] += len(media_list)

        if created_total:
            imported_counts["created"] = created_total
        if updated_total:
            imported_counts["updated"] = updated_total
        if self.skipped_count:
            imported_counts["skipped"] = self.skipped_count

        deduplicated_messages = "\n".join(dict.fromkeys(self.warnings))
        return dict(imported_counts), deduplicated_messages

    def _track_date_added(self, instance, media_type, added_at):
        """Queue an existing row whose Date Added differs from SIMKL's."""
        if added_at is not None and instance.created_at != added_at:
            instance.created_at = added_at
            self.date_added_updates[media_type].append(instance)

    def _apply_date_added(self):
        """Set Date Added to SIMKL's added_to_watchlist_at; return rows touched.

        ``created_at`` is ``auto_now_add``, which bulk_create stamps with the
        import time. A plain bulk_update afterwards does not re-stamp it, and
        writes no history row either.
        """
        touched = 0
        for media_type in DATE_ADDED_TYPES:
            rows = list(self.date_added_updates.get(media_type, []))
            for row in self.bulk_media.get(media_type, []):
                if row.pk is not None and getattr(row, "_source_added_at", None):
                    row.created_at = row._source_added_at
                    rows.append(row)
            if not rows:
                continue
            model = apps.get_model(app_label="app", model_name=media_type)
            helpers.retry_on_lock(
                lambda rows=rows, model=model: model.objects.bulk_update(
                    rows, ["created_at"], batch_size=500
                ),
            )
            touched += len(rows)
        if touched:
            logger.info(
                "Date Added synced from SIMKL on %d row(s) for user %s",
                touched,
                self.user.username,
            )
        return touched

    def _apply_bulk_updates(self):
        """Persist in-place updates with history without altering created_at (Date Added)."""
        update_fields_by_type = {
            MediaTypes.TV.value: [
                "status",
                "score",
                "scored_at",
                "notes",
            ],
            MediaTypes.SEASON.value: ["status"],
            MediaTypes.EPISODE.value: ["end_date"],
            MediaTypes.MOVIE.value: [
                "status",
                "score",
                "scored_at",
                "progress",
                "progressed_at",
                "start_date",
                "end_date",
                "notes",
            ],
            MediaTypes.ANIME.value: [
                "status",
                "score",
                "scored_at",
                "progress",
                "progressed_at",
                "start_date",
                "end_date",
                "notes",
            ],
        }

        for media_type, objs in self.bulk_update.items():
            if not objs:
                continue
            model = apps.get_model(app_label="app", model_name=media_type)
            fields = update_fields_by_type.get(media_type, ["status"])

            def do_update(objs=objs, model=model, fields=fields):
                return bulk_update_with_history(
                    objs,
                    model,
                    fields=fields,
                    batch_size=500,
                    default_user=self.user,
                    default_date=timezone.now(),
                    default_change_reason="Simkl import",
                )

            helpers.retry_on_lock(do_update)
            logger.info(
                "Smart-diff updated %d %s row(s) in-place for user %s",
                len(objs),
                media_type,
                self.user.username,
            )

    def _report_removals(self, data):
        """Hand rows SIMKL no longer lists to the removal tracker."""
        candidates = {}
        for media_type, source in (
            (MediaTypes.TV.value, Sources.TMDB.value),
            (MediaTypes.TV.value, Sources.TVDB.value),
            (MediaTypes.MOVIE.value, Sources.TMDB.value),
            (MediaTypes.ANIME.value, Sources.MAL.value),
        ):
            for media_id, media in self.existing_media[media_type][source].items():
                candidates[f"{media_type}|{source}|{media_id}"] = media
        remote_counts = {
            bucket: len(data.get(bucket) or [])
            for bucket in ("shows", "movies", "anime")
        }
        try:
            warning = remote_removals.sync(
                self.user,
                SOURCE_LABEL,
                remote_counts,
                self.seen_remote,
                candidates,
            )
        except Exception:
            logger.exception(
                "SIMKL removal check failed for user %s", self.user.username
            )
            warning = "Removal check failed this run; see the server log."
        if warning:
            self.warnings.append(warning)

    def _process_media_lists(self, data):
        """Process all media types from Simkl."""
        self._progress_total = (
            len(data.get("shows", []))
            + len(data.get("movies", []))
            + len(data.get("anime", []))
        )
        self._progress_current = 0

        if "shows" in data:
            self._process_tv_list(data["shows"])
        if "movies" in data:
            self._process_movie_list(data["movies"])
        if "anime" in data:
            self._process_anime_list(data["anime"])

    # =========================================================================
    # TV SHOWS PROCESSING (In-Place Updates & Non-Destructive Episode Sync)
    # =========================================================================
    def _process_tv_list(self, tv_list):
        """Process TV list from Simkl with Smart-Diff (never cascade-deletes shows)."""
        logger.info("Processing %d tv shows via Smart-Diff", len(tv_list))
        existing_tv_ids = set()

        for tv in tv_list:
            self._progress_current += 1
            import_progress.report(
                self._progress_current, self._progress_total, "SIMKL"
            )
            try:
                title = tv.get("show", {}).get("title") or "Unknown TV Show"
                logger.debug("Processing TV show: %s", title)

                try:
                    tmdb_id = str(tv["show"]["ids"]["tmdb"])
                except (KeyError, TypeError):
                    self.warnings.append(f"{title}: No TMDB ID found")
                    continue

                tvdb_id_raw = ((tv or {}).get("show") or {}).get("ids", {}).get("tvdb")
                self.seen_remote.add(
                    f"{MediaTypes.TV.value}|{Sources.TMDB.value}|{tmdb_id}"
                )
                if tvdb_id_raw not in (None, ""):
                    self.seen_remote.add(
                        f"{MediaTypes.TV.value}|{Sources.TVDB.value}|{tvdb_id_raw}"
                    )

                if tmdb_id in existing_tv_ids:
                    continue
                existing_tv_ids.add(tmdb_id)

                tv_status = self._get_status(tv.get("status"))
                incoming_score = tv.get("user_rating")
                incoming_notes = (
                    tv.get("memo", {}).get("text", "")
                    if isinstance(tv.get("memo"), dict)
                    else ""
                )
                added_at = self._get_date(tv.get("added_to_watchlist_at"))

                try:
                    season_numbers = [
                        season["number"]
                        for season in tv.get("seasons", [])
                        if isinstance(season, dict) and "number" in season
                    ]
                except (KeyError, TypeError):
                    season_numbers = []

                # Check if this TV show is already tracked in Floppy (TMDB or TVDB fallback)
                existing_tv = None
                tv_source = Sources.TMDB.value
                tv_media_id = tmdb_id

                found_tmdb = self.existing_media[MediaTypes.TV.value][
                    Sources.TMDB.value
                ].get(tmdb_id)
                found_tvdb = (
                    self.existing_media[MediaTypes.TV.value][Sources.TVDB.value].get(
                        str(tvdb_id_raw)
                    )
                    if tvdb_id_raw not in (None, "")
                    else None
                )

                if found_tmdb is not None:
                    existing_tv = found_tmdb
                    tv_source = Sources.TMDB.value
                    tv_media_id = tmdb_id
                elif found_tvdb is not None:
                    existing_tv = found_tvdb
                    tv_source = Sources.TVDB.value
                    tv_media_id = str(tvdb_id_raw)

                metadata = None

                if existing_tv is not None:
                    # =========================================================
                    # 1. SHOW ALREADY EXISTS: IN-PLACE SMART UPDATE
                    # Never delete the show, preserving Date Added (created_at)
                    # and all existing seasons/episodes.
                    # =========================================================
                    tv_instance = existing_tv
                    tv_changed = False

                    if tv_instance.status != tv_status:
                        tv_instance.status = tv_status
                        tv_changed = True

                    if tv_instance.score != incoming_score:
                        tv_instance.score = incoming_score
                        tv_instance.scored_at = (
                            timezone.now() if incoming_score is not None else None
                        )
                        tv_changed = True

                    if (tv_instance.notes or "") != incoming_notes:
                        tv_instance.notes = incoming_notes
                        tv_changed = True

                    self._track_date_added(tv_instance, MediaTypes.TV.value, added_at)

                    if tv_changed:
                        logger.info(
                            "Smart-diff updating TV show '%s' in-place",
                            title,
                        )
                        tv_instance._history_date = self._get_history_date(tv)
                        self.bulk_update[MediaTypes.TV.value].append(tv_instance)
                    else:
                        self.skipped_count += 1

                else:
                    # =========================================================
                    # 2. BRAND NEW TV SHOW: Resolve metadata & Stage for creation
                    # =========================================================
                    try:
                        tv_source, tv_media_id, metadata = self._resolve_tv_metadata(
                            tv,
                            title,
                            tmdb_id,
                            season_numbers,
                        )
                    except services.ProviderAPIError as error:
                        if error.status_code == requests.codes.not_found:
                            self.warnings.append(
                                f"{title}: not found in {Sources.TMDB.label} with ID {tmdb_id}."
                            )
                            continue
                        raise

                    tv_item, _ = helpers.get_or_create_item_across_buckets(
                        preferred_bucket=MediaTypes.TV.value,
                        user=self.user,
                        defaults={
                            **app.models.Item.title_fields_from_metadata(metadata),
                            "image": metadata.get("image", ""),
                        },
                        media_id=tv_media_id,
                        source=tv_source,
                        media_type=MediaTypes.TV.value,
                    )

                    tv_instance = app.models.TV(
                        item=tv_item,
                        user=self.user,
                        status=tv_status,
                        score=incoming_score,
                        notes=incoming_notes,
                    )
                    tv_instance._history_date = self._get_history_date(tv)
                    tv_instance._source_added_at = added_at
                    self.bulk_media[MediaTypes.TV.value].append(tv_instance)

                # =============================================================
                # Process Seasons and Episodes (Append new & sync changed dates)
                # =============================================================
                if season_numbers:
                    # Only fetch provider metadata if there is at least one new season or episode
                    if metadata is None and self._has_new_tv_children(
                        tv, tv_media_id, tv_source
                    ):
                        try:
                            _, _, metadata = self._resolve_tv_metadata(
                                tv,
                                title,
                                tmdb_id,
                                season_numbers,
                            )
                        except Exception as exc:
                            logger.warning(
                                "Could not fetch season metadata for %s: %s",
                                title,
                                exc,
                            )
                            metadata = {}

                    self._process_seasons_and_episodes(
                        tv,
                        tv_instance,
                        tv_media_id,
                        tv_source,
                        metadata,
                        added_at,
                    )

            except Exception as error:
                msg = (
                    f"Error processing TV entry: {tv.get('show', {}).get('title', tv)}"
                )
                raise MediaImportUnexpectedError(msg) from error

        logger.info("Processed %d tv shows cleanly", len(tv_list))

    def _has_new_tv_children(self, tv, tv_media_id, tv_source):
        """Return True if any season or episode in the SIMKL entry is not yet in Floppy."""
        existing_seasons = self.existing_children[MediaTypes.SEASON.value][tv_source]
        existing_episodes = self.existing_children[MediaTypes.EPISODE.value][tv_source]

        for season in tv.get("seasons", []):
            season_number = season.get("number")
            if season_number is None:
                continue
            if (tv_media_id, season_number) not in existing_seasons:
                return True
            for episode in season.get("episodes", []):
                ep_num = episode.get("number")
                if (
                    ep_num is not None
                    and ep_num > 0
                    and (tv_media_id, season_number, ep_num) not in existing_episodes
                ):
                    return True
        return False

    def _resolve_tv_metadata(self, tv, title, tmdb_id, season_numbers):
        """Return the preferred provider payload for a SIMKL TV import entry."""
        metadata = app.providers.tmdb.tv_with_seasons(tmdb_id, season_numbers)

        if not season_numbers or self._has_requested_seasons(metadata, season_numbers):
            return Sources.TMDB.value, tmdb_id, metadata

        tvdb_id = self._get_tvdb_id(tv, metadata)
        if not tvdb_id or not app.providers.tvdb.enabled():
            return Sources.TMDB.value, tmdb_id, metadata

        try:
            tvdb_metadata = app.providers.tvdb.tv_with_seasons(tvdb_id, season_numbers)
        except services.ProviderAPIError as error:
            logger.warning(
                "TVDB fallback failed for SIMKL import %s (%s): %s",
                title,
                tvdb_id,
                error,
            )
            return Sources.TMDB.value, tmdb_id, metadata

        if not self._has_requested_seasons(tvdb_metadata, season_numbers):
            return Sources.TMDB.value, tmdb_id, metadata

        self.warnings.append(f"{title}: imported via {Sources.TVDB.label}.")
        return Sources.TVDB.value, tvdb_id, tvdb_metadata

    def _get_tvdb_id(self, tv, metadata):
        tv_ids = ((tv or {}).get("show") or {}).get("ids") or {}
        tvdb_id = tv_ids.get("tvdb")
        if tvdb_id not in (None, ""):
            return str(tvdb_id)

        metadata_tvdb_id = (metadata or {}).get("tvdb_id")
        if metadata_tvdb_id not in (None, ""):
            return str(metadata_tvdb_id)

        provider_ids = (metadata or {}).get("provider_external_ids") or {}
        provider_tvdb_id = provider_ids.get("tvdb_id")
        if provider_tvdb_id not in (None, ""):
            return str(provider_tvdb_id)

        return None

    def _has_requested_seasons(self, metadata, season_numbers):
        return all(
            (metadata or {}).get(f"season/{season_number}")
            for season_number in season_numbers
        )

    def _child_bucket(self, show_item, default_bucket):
        show_bucket = show_item.library_media_type
        if show_bucket and show_bucket != MediaTypes.TV.value:
            return show_bucket
        return default_bucket

    def _process_seasons_and_episodes(
        self,
        tv,
        tv_instance,
        tv_media_id,
        tv_source,
        metadata,
        added_at=None,
    ):
        """Process seasons and episodes, inserting new ones and updating modified watch dates."""
        for season in tv.get("seasons", []):
            season_number = season.get("number")
            if season_number is None:
                continue

            episodes = season.get("episodes", [])
            season_metadata = (metadata or {}).get(f"season/{season_number}")

            # Check if season already exists locally
            existing_season = self.existing_children[MediaTypes.SEASON.value][
                tv_source
            ].get((tv_media_id, season_number))

            if existing_season is not None:
                season_instance = existing_season
                season_changed = False
                if season_metadata:
                    max_prog = season_metadata.get("max_progress")
                    if episodes and max_prog and episodes[-1].get("number") == max_prog:
                        desired_season_status = Status.COMPLETED.value
                    else:
                        desired_season_status = tv_instance.status
                    if season_instance.status != desired_season_status:
                        season_instance.status = desired_season_status
                        season_changed = True
                self._track_date_added(
                    season_instance, MediaTypes.SEASON.value, added_at
                )
                if season_changed:
                    season_instance._history_date = self._get_history_date(tv)
                    self.bulk_update[MediaTypes.SEASON.value].append(season_instance)
            else:
                if not season_metadata:
                    self.warnings.append(
                        f"{tv_instance.item.title}: missing "
                        f"{Sources(tv_source).label} metadata for season "
                        f"{season_number}.",
                    )
                    continue

                season_image = season_metadata.get("image") or (metadata or {}).get(
                    "image"
                )
                season_bucket = self._child_bucket(
                    tv_instance.item, MediaTypes.SEASON.value
                )
                season_item = helpers.find_item_across_buckets(
                    preferred_bucket=season_bucket,
                    media_id=tv_media_id,
                    source=tv_source,
                    media_type=MediaTypes.SEASON.value,
                    season_number=season_number,
                )
                if season_item is None:
                    season_item, _ = app.models.Item.objects.get_or_create(
                        media_id=tv_media_id,
                        source=tv_source,
                        media_type=MediaTypes.SEASON.value,
                        library_media_type=season_bucket,
                        season_number=season_number,
                        defaults={
                            **app.models.Item.title_fields_from_metadata(
                                metadata or {}
                            ),
                            "image": season_image or "",
                        },
                    )

                max_prog = season_metadata.get("max_progress")
                if episodes and max_prog and episodes[-1].get("number") == max_prog:
                    season_status = Status.COMPLETED.value
                else:
                    season_status = tv_instance.status

                season_instance = app.models.Season(
                    item=season_item,
                    user=self.user,
                    related_tv=tv_instance,
                    status=season_status,
                )
                season_instance._history_date = self._get_history_date(tv)
                season_instance._source_added_at = added_at
                self.bulk_media[MediaTypes.SEASON.value].append(season_instance)
                self.existing_children[MediaTypes.SEASON.value][tv_source][
                    (tv_media_id, season_number)
                ] = season_instance

            # Process episodes: Append newly watched episodes, or update changed watch dates
            for episode in episodes:
                ep_num = episode.get("number")
                if ep_num is None or ep_num <= 0:
                    continue

                watched_at_dt = self._get_date(episode.get("watched_at"))
                ep_identity = (tv_media_id, season_number, ep_num)
                existing_ep = self.existing_children[MediaTypes.EPISODE.value][
                    tv_source
                ].get(ep_identity)

                if existing_ep is not None:
                    # If the episode watch date was updated on SIMKL, sync it in-place
                    if (
                        watched_at_dt is not None
                        and existing_ep.end_date != watched_at_dt
                    ):
                        existing_ep.end_date = watched_at_dt
                        existing_ep._history_date = watched_at_dt
                        self.bulk_update[MediaTypes.EPISODE.value].append(existing_ep)
                    continue

                if not season_metadata:
                    continue

                ep_img = self._get_episode_image(episode, season_number, metadata)
                episode_bucket = self._child_bucket(
                    tv_instance.item, MediaTypes.EPISODE.value
                )
                episode_item = helpers.find_item_across_buckets(
                    preferred_bucket=episode_bucket,
                    media_id=tv_media_id,
                    source=tv_source,
                    media_type=MediaTypes.EPISODE.value,
                    season_number=season_number,
                    episode_number=ep_num,
                )
                if episode_item is None:
                    episode_item, _ = app.models.Item.objects.get_or_create(
                        media_id=tv_media_id,
                        source=tv_source,
                        media_type=MediaTypes.EPISODE.value,
                        library_media_type=episode_bucket,
                        season_number=season_number,
                        episode_number=ep_num,
                        defaults={
                            **app.models.Item.title_fields_from_metadata(
                                metadata or {}
                            ),
                            "image": ep_img or "",
                        },
                    )

                episode_instance = app.models.Episode(
                    item=episode_item,
                    related_season=season_instance,
                    end_date=watched_at_dt,
                )
                episode_instance._history_date = watched_at_dt or timezone.now()
                self.bulk_media[MediaTypes.EPISODE.value].append(episode_instance)
                self.existing_children[MediaTypes.EPISODE.value][tv_source][
                    ep_identity
                ] = episode_instance

    def _get_episode_image(self, episode, season_number, metadata):
        season_metadata = (metadata or {}).get(f"season/{season_number}")
        if not season_metadata:
            return settings.IMG_NONE

        for episode_metadata in season_metadata.get("episodes", []):
            if episode_metadata.get("episode_number") == episode.get("number"):
                if episode_metadata.get("image"):
                    return episode_metadata["image"]
                still_path = episode_metadata.get("still_path")
                if still_path:
                    return f"https://image.tmdb.org/t/p/w500{still_path}"
        return settings.IMG_NONE

    # =========================================================================
    # MOVIES PROCESSING (In-Place Smart-Diff Comparison)
    # =========================================================================
    def _process_movie_list(self, movie_list):
        """Process movie list with Smart-Diff (updates modified movies in-place)."""
        logger.info("Processing %d movies via Smart-Diff", len(movie_list))
        existing_movie_ids = set()

        for movie in movie_list:
            self._progress_current += 1
            import_progress.report(
                self._progress_current, self._progress_total, "SIMKL"
            )
            try:
                title = movie.get("movie", {}).get("title") or "Unknown Movie"
                tmdb_id = movie.get("movie", {}).get("ids", {}).get("tmdb")
                if not tmdb_id:
                    self.warnings.append(f"{title}: No TMDB ID found")
                    continue

                tmdb_id_str = str(tmdb_id)
                self.seen_remote.add(
                    f"{MediaTypes.MOVIE.value}|{Sources.TMDB.value}|{tmdb_id_str}"
                )
                if tmdb_id_str in existing_movie_ids:
                    continue
                existing_movie_ids.add(tmdb_id_str)

                movie_status = self._get_status(movie.get("status"))
                user_rating = movie.get("user_rating")
                memo_text = (
                    movie.get("memo", {}).get("text", "")
                    if isinstance(movie.get("memo"), dict)
                    else ""
                )
                last_watched = self._get_date(movie.get("last_watched_at"))
                added_at = self._get_date(movie.get("added_to_watchlist_at"))
                incoming_progress = 1 if movie_status == Status.COMPLETED.value else 0

                # SMART-DIFF CHECK
                existing = self.existing_media[MediaTypes.MOVIE.value][
                    Sources.TMDB.value
                ].get(tmdb_id_str)

                if existing is not None:
                    status_matches = existing.status == movie_status
                    score_matches = existing.score == user_rating
                    notes_matches = (existing.notes or "") == memo_text
                    progress_matches = (existing.progress or 0) == incoming_progress
                    end_date_matches = (
                        last_watched is None or existing.end_date == last_watched
                    )
                    self._track_date_added(existing, MediaTypes.MOVIE.value, added_at)

                    # If nothing changed, SKIP completely!
                    if (
                        status_matches
                        and score_matches
                        and notes_matches
                        and progress_matches
                        and end_date_matches
                    ):
                        self.skipped_count += 1
                        continue

                    # Update existing Movie row IN-PLACE (preserving Date Added / created_at)
                    logger.info(
                        "Smart-diff updating movie '%s' in-place (status: %s -> %s, score: %s -> %s, date: %s -> %s)",
                        title,
                        existing.status,
                        movie_status,
                        existing.score,
                        user_rating,
                        existing.end_date,
                        last_watched,
                    )
                    if existing.score != user_rating:
                        existing.score = user_rating
                        existing.scored_at = (
                            timezone.now() if user_rating is not None else None
                        )
                    if (existing.progress or 0) != incoming_progress:
                        existing.progress = incoming_progress
                        existing.progressed_at = last_watched or timezone.now()
                    existing.status = movie_status
                    existing.notes = memo_text
                    if last_watched is not None:
                        if existing.start_date is None:
                            existing.start_date = last_watched
                        existing.end_date = last_watched
                    elif movie_status == Status.PLANNING.value:
                        existing.start_date = None
                        existing.end_date = None
                    existing._history_date = self._get_history_date(movie)
                    self.bulk_update[MediaTypes.MOVIE.value].append(existing)
                    continue

                # Brand new movie
                try:
                    metadata = app.providers.tmdb.movie(tmdb_id_str)
                except services.ProviderAPIError as error:
                    if error.status_code == requests.codes.not_found:
                        self.warnings.append(
                            f"{title}: not found in {Sources.TMDB.label} with ID {tmdb_id}."
                        )
                        continue
                    raise

                movie_item, _ = app.models.Item.objects.get_or_create(
                    media_id=tmdb_id_str,
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.MOVIE.value,
                    defaults={
                        **app.models.Item.title_fields_from_metadata(metadata),
                        "image": metadata.get("image", ""),
                    },
                )

                movie_instance = app.models.Movie(
                    item=movie_item,
                    user=self.user,
                    status=movie_status,
                    score=user_rating,
                    progress=incoming_progress,
                    start_date=last_watched,
                    end_date=last_watched,
                    notes=memo_text,
                )
                movie_instance._history_date = self._get_history_date(movie)
                movie_instance._source_added_at = added_at
                self.bulk_media[MediaTypes.MOVIE.value].append(movie_instance)

            except Exception as error:
                msg = f"Error processing movie entry: {movie.get('movie', {}).get('title', movie)}"
                raise MediaImportUnexpectedError(msg) from error

        logger.info("Processed %d movies cleanly", len(movie_list))

    # =========================================================================
    # ANIME PROCESSING (In-Place Smart-Diff on Progress, Status, Score & Dates)
    # =========================================================================
    def _process_anime_list(self, anime_list):
        """Process anime list with Smart-Diff (updates in-place without resetting Date Added)."""
        logger.info("Processing %d anime via Smart-Diff", len(anime_list))
        existing_anime_ids = set()

        for anime in anime_list:
            self._progress_current += 1
            import_progress.report(
                self._progress_current, self._progress_total, "SIMKL"
            )
            try:
                self._process_single_anime_entry(anime, existing_anime_ids)
            except Exception as error:
                msg = f"Error processing anime entry: {anime.get('show', {}).get('title', anime)}"
                raise MediaImportUnexpectedError(msg) from error

        logger.info("Processed %d anime cleanly", len(anime_list))

    def _process_single_anime_entry(self, anime, existing_anime_ids):
        title = anime.get("show", {}).get("title") or "Unknown Anime"
        mal_id = anime.get("show", {}).get("ids", {}).get("mal")

        if not mal_id:
            self.warnings.append(f"{title}: No MyAnimeList ID found")
            return

        mal_id_str = str(mal_id)
        self.seen_remote.add(
            f"{MediaTypes.ANIME.value}|{Sources.MAL.value}|{mal_id_str}"
        )
        if mal_id_str in existing_anime_ids:
            return
        existing_anime_ids.add(mal_id_str)

        anime_status = self._get_status(anime.get("status"))
        incoming_progress = anime.get("watched_episodes_count") or 0
        incoming_score = anime.get("user_rating")
        incoming_notes = (
            anime.get("memo", {}).get("text", "")
            if isinstance(anime.get("memo"), dict)
            else ""
        )
        incoming_start_date = self._get_start_date(anime)
        incoming_end_date = self._get_end_date(
            anime_status,
            anime.get("last_watched_at"),
            anime=anime,
        )
        added_at = self._get_date(anime.get("added_to_watchlist_at"))

        # SMART-DIFF CHECK
        existing = self.existing_media[MediaTypes.ANIME.value][Sources.MAL.value].get(
            mal_id_str
        )

        if existing is not None:
            progress_matches = (existing.progress or 0) == incoming_progress
            status_matches = existing.status == anime_status
            score_matches = existing.score == incoming_score
            notes_matches = (existing.notes or "") == incoming_notes
            start_date_matches = (
                incoming_start_date is None
                or existing.start_date == incoming_start_date
            )
            end_date_matches = (
                incoming_end_date is None or existing.end_date == incoming_end_date
            )
            self._track_date_added(existing, MediaTypes.ANIME.value, added_at)

            # If nothing changed, SKIP completely (0 database writes)!
            if (
                progress_matches
                and status_matches
                and score_matches
                and notes_matches
                and start_date_matches
                and end_date_matches
            ):
                self.skipped_count += 1
                return

            # Update existing Anime row IN-PLACE (preserves Date Added / created_at!)
            logger.info(
                "Smart-diff updating anime '%s' in-place (ep: %s -> %s, status: %s -> %s, score: %s -> %s, last_watched: %s -> %s)",
                title,
                existing.progress,
                incoming_progress,
                existing.status,
                anime_status,
                existing.score,
                incoming_score,
                existing.end_date,
                incoming_end_date,
            )
            if existing.score != incoming_score:
                existing.score = incoming_score
                existing.scored_at = (
                    timezone.now() if incoming_score is not None else None
                )
            if (existing.progress or 0) != incoming_progress:
                existing.progress = incoming_progress
                existing.progressed_at = incoming_end_date or timezone.now()
            existing.status = anime_status
            existing.notes = incoming_notes
            if incoming_start_date is not None:
                existing.start_date = incoming_start_date
            if incoming_end_date is not None:
                existing.end_date = incoming_end_date
            elif anime_status == Status.PLANNING.value:
                existing.end_date = None
            existing._history_date = self._get_history_date(anime)
            self.bulk_update[MediaTypes.ANIME.value].append(existing)
            return

        # Brand new anime
        try:
            metadata = app.providers.mal.anime(mal_id)
        except services.ProviderAPIError as error:
            if error.status_code == requests.codes.not_found:
                self.warnings.append(
                    f"{title}: not found in {Sources.MAL.label} with ID {mal_id}."
                )
                return
            raise

        anime_item, _ = app.models.Item.objects.get_or_create(
            media_id=mal_id_str,
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            defaults={
                **app.models.Item.title_fields_from_metadata(metadata),
                "image": metadata.get("image", ""),
            },
        )

        anime_instance = app.models.Anime(
            item=anime_item,
            user=self.user,
            status=anime_status,
            score=incoming_score,
            progress=incoming_progress,
            start_date=incoming_start_date,
            end_date=incoming_end_date,
            notes=incoming_notes,
        )
        anime_instance._history_date = self._get_history_date(anime)
        anime_instance._source_added_at = added_at
        self.bulk_media[MediaTypes.ANIME.value].append(anime_instance)

    # =========================================================================
    # HELPERS
    # =========================================================================
    def _get_status(self, status):
        status_mapping = {
            "completed": Status.COMPLETED.value,
            "watching": Status.IN_PROGRESS.value,
            "plantowatch": Status.PLANNING.value,
            "hold": Status.PAUSED.value,
            "dropped": Status.DROPPED.value,
        }
        return status_mapping.get(status, Status.IN_PROGRESS.value)

    def _get_date(self, date_str):
        if date_str:
            return parse_datetime(date_str)
        return None

    def _get_start_date(self, anime):
        current_min_date = None
        for season in anime.get("seasons") or []:
            for episode in season.get("episodes") or []:
                date = self._get_date(episode.get("watched_at"))
                if date is not None and (
                    current_min_date is None or date < current_min_date
                ):
                    current_min_date = date
        return current_min_date

    def _get_end_date(self, anime_status, last_watched_at, anime=None):
        """Return Last Watched timestamp for all active/completed statuses.

        In Floppy, ``end_date`` powers the "Last Watched" sort and card label.
        Returning ``last_watched_at`` for In-Progress/Paused/Dropped anime (just
        like TV shows do via max episode end_date) ensures watching anime sort
        properly by Last Watched without waiting until the whole series finishes.
        """
        if anime_status == Status.PLANNING.value:
            return None

        end_dt = self._get_date(last_watched_at)
        if end_dt is not None:
            return end_dt

        if anime:
            current_max_date = None
            for season in anime.get("seasons") or []:
                for episode in season.get("episodes") or []:
                    date = self._get_date(episode.get("watched_at"))
                    if date is not None and (
                        current_max_date is None or date > current_max_date
                    ):
                        current_max_date = date
            return current_max_date

        return None

    def _get_history_date(self, entry):
        if entry.get("last_watched_at"):
            return parse_datetime(entry.get("last_watched_at"))
        if entry.get("added_to_watchlist_at"):
            return parse_datetime(entry.get("added_to_watchlist_at"))
        return timezone.now()
