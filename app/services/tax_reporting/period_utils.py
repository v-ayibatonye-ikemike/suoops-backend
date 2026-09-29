"""Period date range calculation utilities.

Provides functions for calculating date ranges for different
tax reporting periods: day, week, month, year.
"""

from datetime import date, datetime, timedelta
from typing import Optional, Tuple


def calculate_period_range(
    period_type: str,
    year: Optional[int] = None,
    month: Optional[int] = None,
    day: Optional[int] = None,
    week: Optional[int] = None,
) -> Tuple[date, date]:
    """Calculate start_date and end_date for a given period type.

    Args:
        period_type: 'day', 'week', 'month', or 'year'
        year: Required for all period types
        month: Required for 'month' and 'day'
        day: Required for 'day' period type
        week: Required for 'week' period type (ISO week number)

    Returns:
        Tuple of (start_date, end_date) inclusive

    Raises:
        ValueError: If required parameters are missing or invalid
    """
    if not year:
        raise ValueError("year is required for all period types")

    if period_type == "day":
        if not month or not day:
            raise ValueError("month and day required for daily reports")
        try:
            target_date = date(year, month, day)
            return (target_date, target_date)
        except ValueError as e:
            raise ValueError(f"Invalid date: {year}-{month}-{day}") from e

    elif period_type == "week":
        if not week:
            raise ValueError("week number required for weekly reports")
        # ISO 8601 week calculation: week 1 is first week with Thursday
        # Use datetime.fromisocalendar for accurate ISO week dates
        try:
            start_dt = datetime.fromisocalendar(year, week, 1)  # Monday
            end_dt = datetime.fromisocalendar(year, week, 7)  # Sunday
            return (start_dt.date(), end_dt.date())
        except ValueError as e:
            raise ValueError(f"Invalid ISO week: {year}-W{week:02d}") from e

    elif period_type == "month":
        if not month:
            raise ValueError("month required for monthly reports")
        try:
            start_dt = datetime(year, month, 1)
            # Calculate last day of month
            if month == 12:
                end_dt = datetime(year, 12, 31)
            else:
                end_dt = datetime(year, month + 1, 1) - timedelta(days=1)
            return (start_dt.date(), end_dt.date())
        except ValueError as e:
            raise ValueError(f"Invalid month: {year}-{month}") from e

    elif period_type == "year":
        return (date(year, 1, 1), date(year, 12, 31))

    else:
        raise ValueError(f"Invalid period_type: {period_type}. Must be day/week/month/year")
