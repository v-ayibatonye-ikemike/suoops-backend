from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.dependencies import AdminUserDep, CurrentUserDep, DataOwnerDep, DbDep
from app.api.rate_limit import limiter
from app.api.routes_inventory.dependencies import InventoryAccessDep, InventoryAdminDep
from app.core.audit import log_audit_event
from app.core.config import settings
from app.models.schemas import (
    AIAvailabilityOut,
    AIFeedbackIn,
    AIFeedbackOut,
    AITenantPreferencesOut,
    AITenantPreferencesUpdateIn,
    AIUsageOut,
    CollectionDraftOut,
    CollectionDraftUpdateIn,
    CollectionMetricsOut,
    CollectionPrioritiesOut,
    CopilotActionOut,
    CopilotAnswerOut,
    CopilotBriefingOut,
    CopilotDecisionIn,
    CopilotQuestionIn,
    InventoryAdviceOut,
    InventoryPurchaseOrderIn,
    InventoryPurchaseOrderOut,
    StorefrontAdviceOut,
    StorefrontBundleIn,
    StorefrontCopyApplyIn,
    StorefrontCopyDraftOut,
    StorefrontMerchandisingIn,
    StorefrontMerchandisingOut,
    StorefrontProductActionOut,
    StorefrontPromotionIn,
)
from app.services.ai.collections import (
    CollectionConflictError,
    CollectionDeliveryError,
    CollectionsAssistantService,
)
from app.services.ai.copilot import CommerceCopilotService
from app.services.ai.governance import (
    record_feedback,
    tenant_preferences,
    update_tenant_preferences,
)
from app.services.ai.inventory import InventoryAdviceConflictError, InventoryAdviserService
from app.services.ai.storefront import StorefrontAdviceConflictError, StorefrontAdviserService
from app.services.ai.usage import usage_summary

router = APIRouter(prefix="/ai", tags=["ai"])


@router.get("/availability", response_model=AIAvailabilityOut)
def get_ai_availability(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
) -> AIAvailabilityOut:
    preferences = tenant_preferences(db, data_owner_id)
    return AIAvailabilityOut(
        enabled=settings.AI_ENABLED and preferences["enabled"],
        provider=settings.AI_PROVIDER,
        default_model=settings.AI_DEFAULT_MODEL,
    )


@router.get("/usage", response_model=AIUsageOut)
def get_ai_usage(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
) -> AIUsageOut:
    return AIUsageOut(**usage_summary(db, data_owner_id))


@router.get("/preferences", response_model=AITenantPreferencesOut)
def get_ai_preferences(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
) -> AITenantPreferencesOut:
    return AITenantPreferencesOut(**tenant_preferences(db, data_owner_id))


@router.patch("/preferences", response_model=AITenantPreferencesOut)
def update_ai_preferences(
    payload: AITenantPreferencesUpdateIn,
    admin_user_id: AdminUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
) -> AITenantPreferencesOut:
    try:
        result = update_tenant_preferences(
            db,
            data_owner_id=data_owner_id,
            actor_user_id=admin_user_id,
            enabled=payload.enabled,
            feature_overrides=payload.feature_overrides,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    log_audit_event(
        "ai.preferences.updated",
        user_id=admin_user_id,
        data_owner_id=data_owner_id,
        enabled=payload.enabled,
        feature_overrides=payload.feature_overrides,
    )
    return AITenantPreferencesOut(**result)


@router.post("/feedback", response_model=AIFeedbackOut)
@limiter.limit("30/hour")
def submit_ai_feedback(
    request: Request,
    payload: AIFeedbackIn,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
) -> AIFeedbackOut:
    try:
        record_feedback(
            db,
            data_owner_id=data_owner_id,
            actor_user_id=current_user_id,
            feature=payload.feature,
            sentiment=payload.sentiment,
            reason_code=payload.reason_code,
            comment=payload.comment,
            context_id=payload.context_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    log_audit_event(
        "ai.feedback.submitted",
        user_id=current_user_id,
        data_owner_id=data_owner_id,
        feature=payload.feature,
        sentiment=payload.sentiment,
    )
    return AIFeedbackOut(accepted=True)


@router.get("/copilot/briefing", response_model=CopilotBriefingOut)
async def get_copilot_briefing(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    enhance: bool = Query(True),
) -> CopilotBriefingOut:
    result = await CommerceCopilotService(db).daily_briefing(
        actor_user_id=current_user_id,
        data_owner_id=data_owner_id,
        enhance=enhance,
    )
    return CopilotBriefingOut(**result)


@router.post("/copilot/ask", response_model=CopilotAnswerOut)
def ask_copilot(
    payload: CopilotQuestionIn,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
) -> CopilotAnswerOut:
    result = CommerceCopilotService(db).answer_question(payload.question, data_owner_id=data_owner_id)
    return CopilotAnswerOut(**result)


@router.get("/copilot/actions", response_model=list[CopilotActionOut])
def list_copilot_actions(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    status: str = Query("proposed", pattern="^(proposed|accepted|dismissed)$"),
) -> list[CopilotActionOut]:
    service = CommerceCopilotService(db)
    return [CopilotActionOut(**service._action_out(action)) for action in service.list_actions(data_owner_id, status)]


@router.post("/copilot/actions/{action_id}/decision", response_model=CopilotActionOut)
def decide_copilot_action(
    action_id: str,
    payload: CopilotDecisionIn,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
) -> CopilotActionOut:
    try:
        action = CommerceCopilotService(db).decide_action(
            action_id,
            decision=payload.decision,
            actor_user_id=current_user_id,
            data_owner_id=data_owner_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return CopilotActionOut(**CommerceCopilotService._action_out(action))


@router.get("/collections/priorities", response_model=CollectionPrioritiesOut)
def get_collection_priorities(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    limit: int = Query(10, ge=1, le=25),
) -> CollectionPrioritiesOut:
    result = CollectionsAssistantService(db).priorities(
        actor_user_id=current_user_id,
        data_owner_id=data_owner_id,
        limit=limit,
    )
    return CollectionPrioritiesOut(**result)


@router.post("/collections/drafts/{draft_id}/enhance", response_model=CollectionDraftOut)
async def enhance_collection_draft(
    draft_id: str,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
) -> CollectionDraftOut:
    try:
        result = await CollectionsAssistantService(db).enhance_draft(
            draft_id,
            actor_user_id=current_user_id,
            data_owner_id=data_owner_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CollectionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return CollectionDraftOut(**result)


@router.patch("/collections/drafts/{draft_id}", response_model=CollectionDraftOut)
def update_collection_draft(
    draft_id: str,
    payload: CollectionDraftUpdateIn,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
) -> CollectionDraftOut:
    try:
        result = CollectionsAssistantService(db).update_draft(
            draft_id,
            data_owner_id=data_owner_id,
            subject=payload.subject,
            message=payload.message,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CollectionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return CollectionDraftOut(**result)


@router.post("/collections/drafts/{draft_id}/send", response_model=CollectionDraftOut)
async def send_collection_draft(
    draft_id: str,
    payload: CollectionDraftUpdateIn,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
) -> CollectionDraftOut:
    try:
        result = await CollectionsAssistantService(db).send_draft(
            draft_id,
            actor_user_id=current_user_id,
            data_owner_id=data_owner_id,
            subject=payload.subject,
            message=payload.message,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CollectionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CollectionDeliveryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return CollectionDraftOut(**result)


@router.post("/collections/drafts/{draft_id}/dismiss", response_model=CollectionDraftOut)
def dismiss_collection_draft(
    draft_id: str,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
) -> CollectionDraftOut:
    try:
        result = CollectionsAssistantService(db).dismiss_draft(draft_id, data_owner_id=data_owner_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CollectionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return CollectionDraftOut(**result)


@router.get("/collections/metrics", response_model=CollectionMetricsOut)
def get_collection_metrics(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
) -> CollectionMetricsOut:
    return CollectionMetricsOut(**CollectionsAssistantService(db).metrics(data_owner_id))


@router.get("/inventory/advice", response_model=InventoryAdviceOut)
async def get_inventory_advice(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    inventory_access: InventoryAccessDep,
    enhance: bool = Query(False),
) -> InventoryAdviceOut:
    result = await InventoryAdviserService(db).advice(
        actor_user_id=current_user_id,
        data_owner_id=data_owner_id,
        enhance=enhance,
    )
    return InventoryAdviceOut(**result)


@router.post("/inventory/purchase-orders", response_model=InventoryPurchaseOrderOut)
def create_inventory_purchase_order(
    payload: InventoryPurchaseOrderIn,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    inventory_admin: InventoryAdminDep,
) -> InventoryPurchaseOrderOut:
    try:
        result = InventoryAdviserService(db).create_purchase_order(
            payload.product_ids,
            data_owner_id=data_owner_id,
        )
    except InventoryAdviceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return InventoryPurchaseOrderOut(**result)


@router.get("/storefront/advice", response_model=StorefrontAdviceOut)
def get_storefront_advice(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    inventory_access: InventoryAccessDep,
) -> StorefrontAdviceOut:
    return StorefrontAdviceOut(**StorefrontAdviserService(db).advice(data_owner_id))


@router.post("/storefront/products/{product_id}/copy-draft", response_model=StorefrontCopyDraftOut)
async def draft_storefront_product_copy(
    product_id: int,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    inventory_admin: InventoryAdminDep,
) -> StorefrontCopyDraftOut:
    try:
        result = await StorefrontAdviserService(db).draft_copy(
            product_id,
            actor_user_id=current_user_id,
            data_owner_id=data_owner_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return StorefrontCopyDraftOut(**result)


@router.patch("/storefront/products/{product_id}/copy", response_model=StorefrontProductActionOut)
def apply_storefront_product_copy(
    product_id: int,
    payload: StorefrontCopyApplyIn,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    inventory_admin: InventoryAdminDep,
) -> StorefrontProductActionOut:
    try:
        result = StorefrontAdviserService(db).apply_copy(
            product_id,
            payload.description,
            data_owner_id=data_owner_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return StorefrontProductActionOut(**result)


@router.post("/storefront/merchandising", response_model=StorefrontMerchandisingOut)
def apply_storefront_merchandising(
    payload: StorefrontMerchandisingIn,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    inventory_admin: InventoryAdminDep,
) -> StorefrontMerchandisingOut:
    try:
        result = StorefrontAdviserService(db).apply_merchandising(
            payload.product_ids,
            data_owner_id=data_owner_id,
        )
    except StorefrontAdviceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StorefrontMerchandisingOut(**result)


@router.post("/storefront/products/{product_id}/promotion", response_model=StorefrontProductActionOut)
def apply_storefront_promotion(
    product_id: int,
    payload: StorefrontPromotionIn,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    inventory_admin: InventoryAdminDep,
) -> StorefrontProductActionOut:
    try:
        result = StorefrontAdviserService(db).apply_promotion(
            product_id,
            payload.discount_percent,
            data_owner_id=data_owner_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except StorefrontAdviceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StorefrontProductActionOut(**result)


@router.post("/storefront/bundles", response_model=StorefrontMerchandisingOut)
def apply_storefront_bundle(
    payload: StorefrontBundleIn,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    inventory_admin: InventoryAdminDep,
) -> StorefrontMerchandisingOut:
    try:
        result = StorefrontAdviserService(db).apply_bundle(
            payload.product_ids,
            payload.title,
            active=payload.active,
            data_owner_id=data_owner_id,
        )
    except StorefrontAdviceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StorefrontMerchandisingOut(**result)
