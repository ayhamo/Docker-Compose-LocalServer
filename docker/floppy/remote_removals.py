"""Report library rows that a ground-truth remote no longer lists.

An importer that mirrors a remote service (SIMKL, MangaUpdates) never deletes
local rows itself. Instead it reports rows the remote stopped listing, but
only once that is credible:

- A run whose remote counts dropped sharply against the previous run is
  treated as a bad snapshot: nothing is recorded, one warning line says so.
- A row must be missing on ``CONFIRM_RUNS`` consecutive runs before it is
  reported, so a single flaky response never surfaces anything.
- Confirmed rows are kept in a private list named "Not on <source>" so the
  user can review and bulk-delete them; the import message only names a few.

State lives in the cache with no expiry. Losing it only widens the window by
a few runs, which fails toward reporting nothing.
"""

import logging

from django.core.cache import cache

from lists.models import CustomList, CustomListItem

logger = logging.getLogger(__name__)

CONFIRM_RUNS = 3
MIN_SUSPECT_DROP = 20
NAMED_IN_MESSAGE = 5


def state_key(user, source_label):
    """Cache key holding the per-user, per-source removal state."""
    return f"remote_removals:{source_label.lower()}:{user.id}"


def review_list_name(source_label):
    """Name of the list that collects confirmed removals for a source."""
    return f"Not on {source_label}"


def evaluate(user, source_label, remote_counts, seen_keys, candidates):
    """Work out this run's verdict without writing anything.

    Returns ``(skip_message, missing)``: ``skip_message`` is the warning for a
    snapshot that is not trusted (then ``missing`` is empty), otherwise
    ``missing`` maps each absent candidate key to how many consecutive runs it
    has now been absent, this run included.
    """
    state = cache.get(state_key(user, source_label)) or {}
    previous_counts = state.get("counts") or {}
    for bucket, now in remote_counts.items():
        previous = previous_counts.get(bucket)
        if previous is None:
            continue
        if previous - now > max(MIN_SUSPECT_DROP, previous // 10):
            logger.warning(
                "%s returned %d %s for user %s, last run had %d; removal check skipped",
                source_label,
                now,
                bucket,
                user.username,
                previous,
            )
            return (
                f"{source_label} returned {now} {bucket} but the last run had "
                f"{previous}. Removal check skipped this run."
            ), {}

    previous_missing = state.get("missing") or {}
    return None, {
        key: previous_missing.get(key, 0) + 1 for key in candidates.keys() - seen_keys
    }


def sync(user, source_label, remote_counts, seen_keys, candidates):
    """Record this run's missing rows and refresh the review list.

    ``remote_counts`` maps a bucket name to how many entries the remote
    returned for it. ``seen_keys`` holds every ``"type|source|id"`` key the
    remote listed. ``candidates`` maps the same keys to local media rows that
    this importer could have produced. Returns one warning line or ``""``.
    """
    skip_message, missing = evaluate(
        user, source_label, remote_counts, seen_keys, candidates
    )
    if skip_message:
        return skip_message
    cache.set(
        state_key(user, source_label),
        {"counts": dict(remote_counts), "missing": missing},
        timeout=None,
    )

    confirmed = [
        candidates[row_key] for row_key, runs in missing.items() if runs >= CONFIRM_RUNS
    ]
    _sync_review_list(user, source_label, confirmed)
    if not confirmed:
        return ""

    titles = sorted(media.item.title for media in confirmed)
    named = ", ".join(titles[:NAMED_IN_MESSAGE])
    if len(titles) > NAMED_IN_MESSAGE:
        named += f", and {len(titles) - NAMED_IN_MESSAGE} more"
    noun = "entry" if len(titles) == 1 else "entries"
    return (
        f"{len(titles)} {noun} not on {source_label} for {CONFIRM_RUNS} runs: "
        f'{named}. Review and delete them from the list "'
        f'{review_list_name(source_label)}".'
    )


def _sync_review_list(user, source_label, confirmed):
    """Make the review list hold exactly the confirmed rows."""
    name = review_list_name(source_label)
    custom_list = CustomList.objects.filter(owner=user, name=name).first()
    if custom_list is None:
        if not confirmed:
            return
        custom_list = CustomList.objects.create(
            owner=user,
            name=name,
            description=(
                f"Entries {source_label} no longer lists. Filled by the "
                f"{source_label} import; delete what you agree with."
            ),
            visibility="private",
        )

    wanted = {media.item_id for media in confirmed}
    current = set(
        CustomListItem.objects.filter(custom_list=custom_list).values_list(
            "item_id", flat=True
        ),
    )
    for list_item in CustomListItem.objects.filter(
        custom_list=custom_list, item_id__in=current - wanted
    ):
        list_item.delete()
    for item_id in wanted - current:
        CustomListItem.objects.create(
            custom_list=custom_list, item_id=item_id, added_by=user
        )
