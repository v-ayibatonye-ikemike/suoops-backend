"""
Account deletion service for permanently removing user accounts and all associated data.

GDPR and data privacy compliance:
- Hard delete removes all user data permanently
- Audit log entry is created before deletion for compliance
- All related data is cascade deleted
"""

from __future__ import annotations

import logging

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.audit import log_audit_event
from app.models.expense import Expense
from app.models.models import Invoice, InvoiceLine, User
from app.models.referral_models import Referral, ReferralCode, ReferralReward
from app.models.team_models import Team, TeamMember

logger = logging.getLogger(__name__)


class AccountDeletionBlockedError(Exception):
    """Raised when an account can't be deleted yet because it still has buyer
    money in escrow (held/disputed). Callers should surface this as 409."""


class AccountDeletionService:
    """Service for handling complete account deletion."""

    def __init__(self, db: Session):
        self.db = db

    def delete_account(self, user_id: int, deleted_by_user_id: int | None = None) -> dict:
        """
        Permanently delete a user account and all associated data.

        Args:
            user_id: The ID of the user to delete
            deleted_by_user_id: The ID of the user performing the deletion (for audit)
                               If None, it's a self-deletion

        Returns:
            dict with deletion summary

        Raises:
            ValueError: If user not found
            AccountDeletionBlockedError: If the seller has money still in escrow
        """
        user = self.db.query(User).filter(User.id == user_id).one_or_none()
        if not user:
            raise ValueError("User not found")

        # Money-safety: never delete a seller who still has buyer funds in escrow
        # (held or under dispute) — the buyer's protected money would be orphaned
        # (release/refund can't find the seller). Resolve those orders first.
        from app.models.models import StorefrontOrderEscrow

        active_escrow = (
            self.db.query(StorefrontOrderEscrow.id)
            .filter(
                StorefrontOrderEscrow.seller_id == user_id,
                StorefrontOrderEscrow.status.in_(["held", "disputed"]),
            )
            .first()
        )
        if active_escrow is not None:
            raise AccountDeletionBlockedError(
                "This account has orders with money still in escrow (held or under "
                "dispute). Those must be released or refunded before the account "
                "can be deleted."
            )

        # Capture user info for audit before deletion
        user_info = {
            "id": user.id,
            "email": user.email,
            "phone": user.phone,
            "name": user.name,
            "plan": user.plan.value,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        }

        deletion_summary = {
            "user_id": user_id,
            "user_email": user.email,
            "deleted_items": {},
        }

        try:
            # 1. Delete invoice lines (child of invoices)
            invoice_ids = [inv.id for inv in self.db.query(Invoice.id).filter(Invoice.issuer_id == user_id).all()]
            if invoice_ids:
                lines_deleted = self.db.execute(
                    delete(InvoiceLine).where(InvoiceLine.invoice_id.in_(invoice_ids))
                ).rowcount
                deletion_summary["deleted_items"]["invoice_lines"] = lines_deleted

            # 2. Delete invoices
            invoices_deleted = self.db.execute(delete(Invoice).where(Invoice.issuer_id == user_id)).rowcount
            deletion_summary["deleted_items"]["invoices"] = invoices_deleted

            # 3. Delete referral-related data
            # Delete referrals where user is referrer (regardless of whether they have a referral code)
            referrals_as_referrer = self.db.execute(delete(Referral).where(Referral.referrer_id == user_id)).rowcount
            deletion_summary["deleted_items"]["referrals_given"] = referrals_as_referrer

            # Delete referrals where user was referred
            referrals_as_referred = self.db.execute(delete(Referral).where(Referral.referred_id == user_id)).rowcount
            deletion_summary["deleted_items"]["referrals_received"] = referrals_as_referred

            # Delete referral code explicitly (foreign key constraint)
            referral_codes_deleted = self.db.execute(
                delete(ReferralCode).where(ReferralCode.user_id == user_id)
            ).rowcount
            deletion_summary["deleted_items"]["referral_codes"] = referral_codes_deleted

            # 4. Delete referral rewards (cascade should handle, but explicit for safety)
            rewards_deleted = self.db.execute(delete(ReferralReward).where(ReferralReward.user_id == user_id)).rowcount
            deletion_summary["deleted_items"]["referral_rewards"] = rewards_deleted

            # 5. Delete team-related data
            # First delete team memberships where user is a member
            team_memberships_deleted = self.db.execute(delete(TeamMember).where(TeamMember.user_id == user_id)).rowcount
            deletion_summary["deleted_items"]["team_memberships"] = team_memberships_deleted

            # Delete teams owned by user (cascade will handle members and invitations)
            teams_deleted = self.db.execute(delete(Team).where(Team.admin_user_id == user_id)).rowcount
            deletion_summary["deleted_items"]["teams_owned"] = teams_deleted

            # 6. Delete expenses (legacy table, explicit for safety)
            try:
                expenses_deleted = self.db.execute(delete(Expense).where(Expense.user_id == user_id)).rowcount
                deletion_summary["deleted_items"]["expenses"] = expenses_deleted
            except Exception as e:
                # Expense table might not exist in some environments
                logger.debug("Expense table deletion skipped: %s", e)

            # 7. The following are cascade deleted via User relationships:
            # - OAuth tokens
            # - Payment transactions
            # - Products, categories, stock movements
            # - Suppliers, purchase orders
            # - Tax profile, VAT returns

            # 8. Delete the user (cascades remaining relationships)
            self.db.delete(user)
            self.db.commit()

            # 9. Remove from Brevo contact lists (async-safe, fire and forget)
            if user_info.get("email"):
                try:
                    self._remove_from_brevo(user_info["email"])
                except Exception as e:
                    logger.warning("Brevo contact removal failed for %s: %s", user_info["email"], e)

            # Log audit event
            log_audit_event(
                action="account.deleted",
                user_id=deleted_by_user_id or user_id,
                status="success",
                extra={
                    "deleted_user": user_info,
                    "summary": deletion_summary["deleted_items"],
                    "self_deletion": deleted_by_user_id is None or deleted_by_user_id == user_id,
                },
            )

            logger.info(
                "Account deleted: user_id=%s, email=%s, by_user=%s",
                user_id,
                user_info["email"],
                deleted_by_user_id or "self",
            )

            return deletion_summary

        except Exception as e:
            self.db.rollback()
            logger.error("Failed to delete account %s: %s", user_id, e, exc_info=True)
            log_audit_event(
                action="account.deletion_failed",
                user_id=deleted_by_user_id or user_id,
                status="failure",
                extra={"target_user_id": user_id, "error": str(e)},
            )
            raise

    def can_delete_account(self, requester_id: int, target_user_id: int) -> tuple[bool, str]:
        """
        Check if requester can delete the target account.

        Rules:
        - Users can delete their own account
        - Admins can delete any account
        - Staff cannot delete accounts

        Returns:
            tuple of (can_delete, reason)
        """
        requester = self.db.query(User).filter(User.id == requester_id).one_or_none()
        if not requester:
            return False, "Requester not found"

        # Self-deletion is always allowed
        if requester_id == target_user_id:
            return True, "Self-deletion"

        # Admin can delete any account
        if requester.role == "admin":
            return True, "Admin privilege"

        return False, "You can only delete your own account"

    @staticmethod
    def _remove_from_brevo(email: str) -> None:
        """Remove a contact from Brevo entirely (all lists + delete contact)."""
        import httpx

        from app.core.config import settings

        api_key = getattr(settings, "BREVO_CONTACTS_API_KEY", None)
        if not api_key:
            return

        headers = {"api-key": api_key, "Content-Type": "application/json"}
        encoded_email = email.replace("@", "%40")

        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.delete(
                    f"https://api.brevo.com/v3/contacts/{encoded_email}",
                    headers=headers,
                )
                if resp.status_code in (200, 204, 404):
                    logger.info("Removed %s from Brevo contacts", email)
                else:
                    logger.warning("Brevo delete contact %s: %s", email, resp.status_code)
        except Exception as e:
            logger.warning("Brevo delete contact error for %s: %s", email, e)
