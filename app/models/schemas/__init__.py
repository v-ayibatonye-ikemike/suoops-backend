"""Pydantic schemas for API requests and responses.

Refactored from monolithic schemas.py for SRP compliance.

Sub-modules:
- invoice: Invoice-related schemas
- auth: Authentication schemas
- business: Bank, OAuth, OCR schemas
- analytics: Analytics schemas
- utils: Common utility functions
"""

# Invoice schemas
# Analytics schemas
from .ai import AIAvailabilityOut, AIUsageFeatureOut, AIUsageOut
from .ai_governance import (
    AIFeatureControlOut,
    AIFeatureControlUpdateIn,
    AIFeatureMetricOut,
    AIFeedbackIn,
    AIFeedbackOut,
    AIGovernanceOverviewOut,
    AITenantPreferencesOut,
    AITenantPreferencesUpdateIn,
)
from .analytics import (
    ActivityMixOut,
    AgingReport,
    AnalyticsDashboard,
    BusinessSnapshotOut,
    CustomerMetrics,
    DataProvenanceOut,
    FulfillmentReliabilityOut,
    InvoiceMetrics,
    MonthlyTrend,
    PaymentReliabilityOut,
    RevenueConsistencyOut,
    RevenueMetrics,
    TaxComplianceOut,
)

# Auth schemas
from .auth import (
    LoginVerify,
    MessageOut,
    OTPEmailRequest,
    OTPPhoneRequest,
    OTPResend,
    PhoneVerificationRequest,
    PhoneVerificationResponse,
    PhoneVerificationVerify,
    RefreshRequest,
    SignupStart,
    SignupVerify,
    TokenOut,
    UserOut,
)

# Business schemas
from .business import (
    BankDetailsOut,
    BankDetailsUpdate,
    OAuthCallbackOut,
    OAuthProviderInfo,
    OAuthProvidersOut,
    OCRItemOut,
    OCRParseOut,
)
from .buyer_ai import BuyerProductMatchOut, BuyerShoppingRequest, BuyerShoppingResponse
from .collections_ai import (
    CollectionDraftOut,
    CollectionDraftUpdateIn,
    CollectionMetricsOut,
    CollectionPrioritiesOut,
)
from .copilot import (
    CopilotActionOut,
    CopilotAnswerOut,
    CopilotBriefingOut,
    CopilotDecisionIn,
    CopilotQuestionIn,
)
from .dispute_ai import DisputeAssistantOut, DisputeEvidenceOut, DisputeTimelineEventOut
from .inventory_ai import (
    InventoryAdviceOut,
    InventoryPurchaseOrderIn,
    InventoryPurchaseOrderOut,
    InventoryRecommendationOut,
)
from .invoice import (
    CustomerOut,
    InvoiceCreate,
    InvoiceLineIn,
    InvoiceLineOut,
    InvoiceOut,
    InvoiceOutDetailed,
    InvoicePackPurchaseInitOut,
    InvoicePublicOut,
    InvoiceQuotaOut,
    InvoiceStatusUpdate,
    InvoiceVerificationItem,
    InvoiceVerificationOut,
    PaginatedResponse,
    QuickSaleCreate,
    ReceiptUploadOut,
)
from .storefront_ai import (
    StorefrontAdviceOut,
    StorefrontBundleIn,
    StorefrontCopyApplyIn,
    StorefrontCopyDraftOut,
    StorefrontMerchandisingIn,
    StorefrontMerchandisingOut,
    StorefrontProductActionOut,
    StorefrontPromotionIn,
)

__all__ = [
    # Invoice
    "InvoiceLineIn",
    "InvoiceCreate",
    "QuickSaleCreate",
    "CustomerOut",
    "InvoiceOut",
    "InvoiceLineOut",
    "InvoiceOutDetailed",
    "InvoiceStatusUpdate",
    "InvoicePublicOut",
    "InvoiceVerificationItem",
    "InvoiceVerificationOut",
    "InvoiceQuotaOut",
    "InvoicePackPurchaseInitOut",
    "ReceiptUploadOut",
    "PaginatedResponse",
    # Auth
    "OTPPhoneRequest",
    "OTPEmailRequest",
    "SignupStart",
    "SignupVerify",
    "LoginVerify",
    "OTPResend",
    "UserOut",
    "TokenOut",
    "RefreshRequest",
    "MessageOut",
    "PhoneVerificationRequest",
    "PhoneVerificationVerify",
    "PhoneVerificationResponse",
    # Business
    "BankDetailsUpdate",
    "BankDetailsOut",
    "OCRItemOut",
    "OCRParseOut",
    "OAuthProviderInfo",
    "OAuthProvidersOut",
    "OAuthCallbackOut",
    # Analytics
    "RevenueMetrics",
    "InvoiceMetrics",
    "CustomerMetrics",
    "AgingReport",
    "MonthlyTrend",
    "AnalyticsDashboard",
    "BusinessSnapshotOut",
    "PaymentReliabilityOut",
    "RevenueConsistencyOut",
    "TaxComplianceOut",
    "ActivityMixOut",
    "FulfillmentReliabilityOut",
    "DataProvenanceOut",
    # AI
    "AIAvailabilityOut",
    "AIUsageFeatureOut",
    "AIUsageOut",
    "AIFeatureControlOut",
    "AIFeatureControlUpdateIn",
    "AIFeatureMetricOut",
    "AIFeedbackIn",
    "AIFeedbackOut",
    "AIGovernanceOverviewOut",
    "AITenantPreferencesOut",
    "AITenantPreferencesUpdateIn",
    "BuyerProductMatchOut",
    "BuyerShoppingRequest",
    "BuyerShoppingResponse",
    "DisputeAssistantOut",
    "DisputeEvidenceOut",
    "DisputeTimelineEventOut",
    "CopilotActionOut",
    "CopilotAnswerOut",
    "CopilotBriefingOut",
    "CopilotDecisionIn",
    "CopilotQuestionIn",
    "CollectionDraftOut",
    "CollectionDraftUpdateIn",
    "CollectionMetricsOut",
    "CollectionPrioritiesOut",
    "InventoryAdviceOut",
    "InventoryPurchaseOrderIn",
    "InventoryPurchaseOrderOut",
    "InventoryRecommendationOut",
    "StorefrontAdviceOut",
    "StorefrontBundleIn",
    "StorefrontCopyApplyIn",
    "StorefrontCopyDraftOut",
    "StorefrontMerchandisingIn",
    "StorefrontMerchandisingOut",
    "StorefrontProductActionOut",
    "StorefrontPromotionIn",
]
