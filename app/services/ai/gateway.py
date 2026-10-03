from __future__ import annotations

import datetime as dt
import json
import logging
import time
import uuid
from typing import TypeVar, cast

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ai_models import AIUsageEvent

from .governance import ai_access_allowed
from .provider import AIProvider, build_ai_provider
from .redaction import hash_messages, redact_messages
from .types import AIRequest
from .usage import estimate_cost_usd, reserve_operation

logger = logging.getLogger(__name__)
OutputT = TypeVar("OutputT", bound=BaseModel)


class AIGatewayError(RuntimeError):
    code = "ai_gateway_error"


class AIUnavailableError(AIGatewayError):
    code = "ai_unavailable"


class AIQuotaExceededError(AIGatewayError):
    code = "ai_quota_exceeded"


class AIProviderError(AIGatewayError):
    code = "ai_provider_error"


class AIResponseValidationError(AIGatewayError):
    code = "ai_response_validation_error"


class AIGateway:
    def __init__(self, db: Session, *, provider: AIProvider | None = None) -> None:
        self._db = db
        self._provider = provider

    async def generate_structured(
        self,
        request: AIRequest,
        output_type: type[OutputT],
        *,
        actor_user_id: int | None = None,
        actor_admin_user_id: int | None = None,
        data_owner_id: int,
        enforce_owner_quota: bool = True,
    ) -> OutputT:
        if not settings.AI_ENABLED:
            raise AIUnavailableError("AI features are currently disabled")
        if not request.feature.strip() or not request.prompt_version.strip() or not request.messages:
            raise ValueError("feature, prompt_version, and messages are required")
        if (actor_user_id is None) == (actor_admin_user_id is None):
            raise ValueError("exactly one AI actor is required")

        model = request.model or settings.AI_DEFAULT_MODEL
        messages = redact_messages(request.messages)
        allowed, denial_reason = ai_access_allowed(
            self._db,
            feature=request.feature,
            data_owner_id=data_owner_id,
        )
        if not allowed:
            self._db.add(
                AIUsageEvent(
                    operation_id=str(uuid.uuid4()),
                    data_owner_id=data_owner_id,
                    actor_user_id=actor_user_id,
                    actor_admin_user_id=actor_admin_user_id,
                    counts_toward_quota=False,
                    feature=request.feature,
                    provider=settings.AI_PROVIDER,
                    model=model,
                    prompt_version=request.prompt_version,
                    status="blocked",
                    input_hash=hash_messages(messages),
                    error_code=denial_reason,
                    details={"metadata": request.metadata},
                    completed_at=dt.datetime.now(dt.timezone.utc),
                )
            )
            self._db.commit()
            raise AIUnavailableError("AI feature is not enabled for this workspace")

        provider = self._provider or build_ai_provider()
        event = AIUsageEvent(
            operation_id=str(uuid.uuid4()),
            data_owner_id=data_owner_id,
            actor_user_id=actor_user_id,
            actor_admin_user_id=actor_admin_user_id,
            counts_toward_quota=enforce_owner_quota,
            feature=request.feature,
            provider=provider.name,
            model=model,
            prompt_version=request.prompt_version,
            status="processing",
            input_hash=hash_messages(messages),
            details={"metadata": request.metadata},
        )
        if enforce_owner_quota:
            try:
                reserve_operation(self._db, event, settings.AI_MONTHLY_INCLUDED_OPERATIONS)
            except ValueError as exc:
                if str(exc) == "monthly_ai_quota_exceeded":
                    raise AIQuotaExceededError("Monthly AI assist limit reached") from exc
                raise

        started = time.monotonic()
        try:
            completion = await provider.complete(
                messages=messages,
                model=model,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                structured=True,
            )
            try:
                result = cast(OutputT, output_type.model_validate(json.loads(completion.content)))
            except (json.JSONDecodeError, ValidationError, TypeError) as exc:
                raise AIResponseValidationError("AI response did not match the required schema") from exc

            event.status = "succeeded"
            event.provider = completion.provider
            event.model = completion.model
            event.input_tokens = completion.input_tokens
            event.output_tokens = completion.output_tokens
            event.estimated_cost_usd = estimate_cost_usd(completion.input_tokens, completion.output_tokens)
            return result
        except AIResponseValidationError as exc:
            event.status = "failed"
            event.error_code = exc.code
            raise
        except Exception as exc:
            event.status = "failed"
            event.error_code = AIProviderError.code
            logger.warning("AI provider operation failed feature=%s operation=%s", request.feature, event.operation_id)
            raise AIProviderError("AI provider request failed") from exc
        finally:
            event.duration_ms = int((time.monotonic() - started) * 1000)
            event.completed_at = dt.datetime.now(dt.timezone.utc)
            self._db.add(event)
            self._db.commit()
