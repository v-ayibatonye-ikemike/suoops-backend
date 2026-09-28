"""Auto-promotion of opted-in storefront products on SuoOps's own social
media (Facebook/Instagram) — a curated daily batch, not real-time posting.

Sub-modules:
- eligibility_service: which products get featured today, and in what order
- caption_service: LLM-generated caption + UTM-tagged storefront link
- meta_client: the actual Graph API calls (Facebook Page + Instagram)
- service: orchestrates the three above into the daily run
"""
