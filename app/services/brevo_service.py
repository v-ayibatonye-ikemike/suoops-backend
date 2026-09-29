"""Brevo (Sendinblue) contact sync service.

Handles real-time syncing of users to Brevo contact lists.
"""

import logging
import urllib.parse
from typing import TYPE_CHECKING, Optional

import httpx

from app.core.config import settings

if TYPE_CHECKING:
    from app.models import models

logger = logging.getLogger(__name__)


def _contact_sync_enabled() -> bool:
    """Marketing contact sync to Brevo is retired (moved to Zoho Campaigns).

    OFF by default; set ``BREVO_CONTACT_SYNC_ENABLED=true`` to re-enable. Brevo
    remains available purely as a transactional-email fallback either way.
    """
    return bool(getattr(settings, "BREVO_CONTACT_SYNC_ENABLED", False))


# Brevo list IDs (matching your Brevo setup)
BREVO_LIST_ALL_USERS = 12  # All Users (master list)
BREVO_LIST_STARTER = 9  # Legacy Starter list (now merged with Free/Active)
BREVO_LIST_PRO = 10  # Pro Users (Retention)
BREVO_LIST_ACTIVE_FREE = 7  # Active Free/Starter Users
BREVO_LIST_LOW_BALANCE = 6  # Low Balance Users
BREVO_LIST_INACTIVE = 5  # Inactive Users

# Plan to segment list mapping
# STARTER plan removed — "starter" maps to Active Free list for legacy compatibility
PLAN_TO_LIST_MAP = {
    "FREE": BREVO_LIST_ACTIVE_FREE,
    "STARTER": BREVO_LIST_ACTIVE_FREE,  # Legacy: treat as FREE
    "PRO": BREVO_LIST_PRO,
}

# All segment lists (used for removal when switching plans)
ALL_SEGMENT_LISTS = [
    BREVO_LIST_STARTER,  # Keep for removal of legacy contacts
    BREVO_LIST_PRO,
    BREVO_LIST_ACTIVE_FREE,
]


def get_segment_list_for_plan(plan_value: str) -> Optional[int]:
    """Get the appropriate Brevo segment list ID for a user's plan."""
    return PLAN_TO_LIST_MAP.get(plan_value.upper())


async def remove_from_lists(
    email: str, list_ids: list[int], brevo_api_key: str, client: httpx.AsyncClient | None = None
) -> bool:
    """Remove a contact from specified Brevo lists."""
    if not list_ids:
        return True

    headers = {"api-key": brevo_api_key, "Content-Type": "application/json"}

    async def _do(c: httpx.AsyncClient) -> bool:
        for list_id in list_ids:
            try:
                response = await c.post(
                    f"https://api.brevo.com/v3/contacts/lists/{list_id}/contacts/remove",
                    headers=headers,
                    json={"emails": [email]},
                    timeout=10.0,
                )
                if response.status_code not in (200, 201, 204, 404):
                    logger.warning(
                        "Failed to remove %s from list %d: %s",
                        email,
                        list_id,
                        response.status_code,
                    )
            except Exception as e:
                logger.warning("Error removing %s from list %d: %s", email, list_id, e)
        return True

    if client:
        return await _do(client)
    async with httpx.AsyncClient() as c:
        return await _do(c)


async def add_to_list(email: str, list_id: int, brevo_api_key: str) -> bool:
    """Add a contact to a specific Brevo list."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://api.brevo.com/v3/contacts/lists/{list_id}/contacts/add",
                headers={"api-key": brevo_api_key, "Content-Type": "application/json"},
                json={"emails": [email]},
                timeout=10.0,
            )
            if response.status_code in (200, 201, 204):
                logger.info("Added %s to Brevo list %d", email, list_id)
                return True
            else:
                logger.warning(
                    "Failed to add %s to list %d: %s - %s", email, list_id, response.status_code, response.text
                )
                return False
    except Exception as e:
        logger.warning("Error adding %s to list %d: %s", email, list_id, e)
        return False


async def sync_user_to_brevo(user: "models.User") -> bool:
    """
    Sync a single user to Brevo lists:
    1. Master "All Users" list (always)
    2. Plan-specific segment list (FREE→#7, PRO→#10)

    Called on:
    - User signup (new user)
    - User plan change (update attributes + segment)

    Returns True if successful, False otherwise.
    """
    if not _contact_sync_enabled():
        return False
    brevo_api_key = getattr(settings, "BREVO_CONTACTS_API_KEY", None)
    if not brevo_api_key:
        logger.debug("BREVO_CONTACTS_API_KEY not configured, skipping sync")
        return False

    if not user.email:
        logger.debug("User %s has no email, skipping Brevo sync", user.id)
        return False

    # Determine which segment list to add user to
    plan_value = user.plan.value if hasattr(user.plan, "value") else str(user.plan)
    segment_list_id = get_segment_list_for_plan(plan_value)

    # Build list of lists to add user to
    list_ids = [BREVO_LIST_ALL_USERS]
    if segment_list_id:
        list_ids.append(segment_list_id)

    contact_data = {
        "email": user.email,
        "attributes": {
            "FIRSTNAME": user.name or "Customer",
            "PHONE": user.phone,
            "PLAN": plan_value,
            "INVOICE_BALANCE": getattr(user, "invoice_balance", 5),
            "BUSINESS_NAME": user.business_name or "",
        },
        "listIds": list_ids,
        "updateEnabled": True,  # Update if contact exists
    }

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.brevo.com/v3/contacts",
                headers={"api-key": brevo_api_key, "Content-Type": "application/json"},
                json=contact_data,
                timeout=10.0,
            )

            if response.status_code in (200, 201, 204):
                logger.info("Synced user %s to Brevo lists %s", user.email, list_ids)
                return True
            elif response.status_code == 400 and "Contact already exist" in response.text:
                # Contact exists, try to update (with segment switch)
                return await update_brevo_contact(user)
            else:
                logger.warning("Brevo sync failed for %s: %s - %s", user.email, response.status_code, response.text)
                return False

    except Exception as e:
        logger.warning("Brevo sync error for %s: %s", user.email, e)
        return False


async def update_brevo_contact(user: "models.User") -> bool:
    """
    Update existing contact in Brevo.

    Removes user from old segment lists and adds to new one based on plan.
    Uses a single httpx.AsyncClient for all requests to avoid connection churn.
    """
    brevo_api_key = getattr(settings, "BREVO_CONTACTS_API_KEY", None)
    if not brevo_api_key or not user.email:
        return False

    # Determine which segment list to add user to
    plan_value = user.plan.value if hasattr(user.plan, "value") else str(user.plan)
    segment_list_id = get_segment_list_for_plan(plan_value)

    # Remove from all segment lists first (to handle plan changes)
    lists_to_remove = [lid for lid in ALL_SEGMENT_LISTS if lid != segment_list_id]

    # Build list of lists to add to
    list_ids = [BREVO_LIST_ALL_USERS]
    if segment_list_id:
        list_ids.append(segment_list_id)

    update_data = {
        "attributes": {
            "FIRSTNAME": user.name or "Customer",
            "PHONE": user.phone,
            "PLAN": plan_value,
            "INVOICE_BALANCE": getattr(user, "invoice_balance", 5),
            "BUSINESS_NAME": user.business_name or "",
        },
        "listIds": list_ids,
    }

    try:
        async with httpx.AsyncClient() as client:
            # Remove from old lists and update contact in same connection
            await remove_from_lists(user.email, lists_to_remove, brevo_api_key, client=client)

            encoded_email = urllib.parse.quote(user.email, safe="")

            response = await client.put(
                f"https://api.brevo.com/v3/contacts/{encoded_email}",
                headers={"api-key": brevo_api_key, "Content-Type": "application/json"},
                json=update_data,
                timeout=10.0,
            )

            if response.status_code in (200, 204):
                logger.info("Updated Brevo contact %s (lists: %s)", user.email, list_ids)
                return True
            else:
                logger.warning("Brevo update failed for %s: %s", user.email, response.status_code)
                return False

    except Exception as e:
        logger.warning("Brevo update error for %s: %s", user.email, e)
        return False


async def sync_low_balance_status(user: "models.User") -> bool:
    """
    Add/remove user from Low Balance list based on invoice_balance.

    Called when user's balance changes (e.g., after using invoices or topping up).
    """
    if not _contact_sync_enabled():
        return False
    brevo_api_key = getattr(settings, "BREVO_CONTACTS_API_KEY", None)
    if not brevo_api_key or not user.email:
        return False

    balance = getattr(user, "invoice_balance", 5)
    LOW_BALANCE_THRESHOLD = 2  # Consider low if <= 2 invoices remaining

    try:
        if balance <= LOW_BALANCE_THRESHOLD:
            # Add to low balance list
            return await add_to_list(user.email, BREVO_LIST_LOW_BALANCE, brevo_api_key)
        else:
            # Remove from low balance list
            return await remove_from_lists(user.email, [BREVO_LIST_LOW_BALANCE], brevo_api_key)
    except Exception as e:
        logger.warning("Low balance sync error for %s: %s", user.email, e)
        return False


def get_all_contacts_sync(list_id: int = BREVO_LIST_ALL_USERS) -> Optional[dict[str, bool]]:
    """Return a mapping of {lowercased email: is_blocklisted} for a Brevo list.

    Paginates through the Brevo list-contacts endpoint (max 500 per page).
    The blocklist flag lets callers preserve suppressed contacts (hard bounces /
    unsubscribes) instead of deleting them and losing their suppression state.

    Returns None if the API key is missing or a request fails (so callers can
    abort the reconcile safely rather than treating an error as "no contacts").
    """
    if not _contact_sync_enabled():
        return None
    brevo_api_key = getattr(settings, "BREVO_CONTACTS_API_KEY", None)
    if not brevo_api_key:
        logger.debug("BREVO_CONTACTS_API_KEY not configured, skipping contact fetch")
        return None

    headers = {"api-key": brevo_api_key, "Content-Type": "application/json"}
    contacts: dict[str, bool] = {}
    limit = 500
    offset = 0

    try:
        with httpx.Client(timeout=30.0) as client:
            while True:
                resp = client.get(
                    f"https://api.brevo.com/v3/contacts/lists/{list_id}/contacts",
                    headers=headers,
                    params={"limit": limit, "offset": offset},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "Brevo list %d fetch failed at offset %d: %s",
                        list_id,
                        offset,
                        resp.status_code,
                    )
                    return None
                data = resp.json()
                page = data.get("contacts", [])
                for c in page:
                    email = (c.get("email") or "").strip().lower()
                    if email:
                        contacts[email] = bool(c.get("emailBlacklisted", False))
                if len(page) < limit:
                    break
                offset += limit
    except Exception as e:
        logger.warning("Brevo list %d fetch error: %s", list_id, e)
        return None

    return contacts


def delete_contact_sync(email: str) -> bool:
    """Delete a contact from Brevo entirely. Returns True on success/absent."""
    brevo_api_key = getattr(settings, "BREVO_CONTACTS_API_KEY", None)
    if not brevo_api_key or not email:
        return False

    headers = {"api-key": brevo_api_key, "Content-Type": "application/json"}
    encoded_email = urllib.parse.quote(email, safe="")

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.delete(
                f"https://api.brevo.com/v3/contacts/{encoded_email}",
                headers=headers,
            )
            if resp.status_code in (200, 204, 404):
                return True
            logger.warning("Brevo delete contact %s: %s", email, resp.status_code)
            return False
    except Exception as e:
        logger.warning("Brevo delete contact error for %s: %s", email, e)
        return False


def sync_user_to_brevo_sync(user: "models.User") -> bool:
    """
    Synchronous wrapper for sync_user_to_brevo.

    Use this in synchronous contexts (like auth_service).
    Runs the async function in a new event loop.
    """
    import asyncio

    try:
        # Check if we're already in an async context
        try:
            asyncio.get_running_loop()
            # We're in an async context, create a task via thread to avoid nested loop issues
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, sync_user_to_brevo(user))
                return future.result(timeout=15)
        except RuntimeError:
            # No running loop, safe to use asyncio.run
            return asyncio.run(sync_user_to_brevo(user))
    except Exception as e:
        logger.warning("Sync wrapper error: %s", e)
        return False
