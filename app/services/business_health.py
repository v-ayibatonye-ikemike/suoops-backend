def business_health_score(
    days_since: int | None,
    revenue_count: int,
    collection_rate: float,
    recent_invoices: int,
    invoice_count: int,
) -> int:
    """Shared Business Intelligence score; lifecycle uses meaningful activity recency."""
    score = 50
    if days_since is None:
        score -= 25
    elif days_since <= 3:
        score += 30
    elif days_since <= 7:
        score += 20
    elif days_since <= 14:
        score += 10
    elif days_since > 60:
        score -= 20
    elif days_since > 30:
        score -= 10
    if revenue_count >= 3:
        score += 15 if collection_rate >= 70 else (5 if collection_rate >= 40 else -10)
    if recent_invoices >= 10:
        score += 10
    elif recent_invoices >= 3:
        score += 5
    elif invoice_count == 0:
        score -= 5
    return max(0, min(100, score))
