"""
Expense NLP extraction service.

Extracts expense details from natural language text (WhatsApp/email messages).
Handles Nigerian English patterns and currency formatting.

Examples:
- "Expense: ₦2,000 for internet data on Nov 10"
- "₦5,000 market rent November"
- "Paid ₦15,000 for shop rent today"
- "Spent 3000 naira on transport yesterday"
"""

import logging
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import TypedDict

logger = logging.getLogger(__name__)


class ExpenseData(TypedDict):
    """Extracted expense information"""

    amount: Decimal
    date: date | None
    category: str
    description: str
    merchant: str | None


class ExpenseNLPService:
    """Extract expense details from natural language"""

    # Category keywords (Nigerian context)
    CATEGORY_KEYWORDS = {
        "rent": ["rent", "lease", "tenancy", "shop", "office", "store", "space"],
        "utilities": ["electricity", "water", "nepa", "phcn", "prepaid", "meter", "utility"],
        "data_internet": [
            "data",
            "internet",
            "airtime",
            "network",
            "mtn",
            "glo",
            "airtel",
            "9mobile",
            "wifi",
            "broadband",
            "bundle",
        ],
        "transport": [
            "transport",
            "fuel",
            "petrol",
            "diesel",
            "gas",
            "taxi",
            "uber",
            "bolt",
            "okada",
            "keke",
            "danfo",
            "bus fare",
            "vehicle",
        ],
        "supplies": [
            "supplies",
            "ink",
            "paper",
            "stationery",
            "market",
            "stock",
            "inventory",
            "materials",
            "goods",
            "items",
        ],
        "equipment": [
            "equipment",
            "laptop",
            "computer",
            "phone",
            "printer",
            "scanner",
            "tools",
            "machine",
            "generator",
        ],
        "marketing": [
            "ads",
            "advertising",
            "advert",
            "promotion",
            "marketing",
            "branding",
            "facebook",
            "instagram",
            "google ads",
            "flyers",
            "banner",
        ],
        "professional_fees": [
            "accountant",
            "lawyer",
            "consultant",
            "professional",
            "attorney",
            "auditor",
            "legal",
            "accounting",
        ],
        "staff_wages": ["salary", "wage", "staff", "employee", "worker", "helper", "assistant", "contract", "labor"],
        "maintenance": ["repair", "maintenance", "fixing", "service", "upkeep", "plumber", "electrician", "mechanic"],
    }

    # Date keywords
    DATE_KEYWORDS = {
        "today": 0,
        "yesterday": -1,
        "last week": -7,
        "last month": -30,
    }

    # Month names
    MONTHS = {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "september": 9,
        "sept": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "dec": 12,
        "december": 12,
    }

    def extract_expense(self, text: str) -> ExpenseData:
        """
        Extract expense details from natural language text.

        Args:
            text: Natural language text (e.g., "₦2,000 for data on Nov 10")

        Returns:
            Extracted expense data
        """
        text_lower = text.lower()

        # Extract amount
        amount = self._extract_amount(text)

        # Extract date
        date_obj = self._extract_date(text_lower)

        # Categorize
        category = self._categorize(text_lower)

        # Extract description & merchant
        description = self._clean_description(text)
        merchant = self._extract_merchant(text)

        logger.info(
            f"Extracted expense: amount={amount}, date={date_obj}, " f"category={category}, description={description}"
        )

        return ExpenseData(
            amount=amount,
            date=date_obj,
            category=category,
            description=description,
            merchant=merchant,
        )

    def _extract_amount(self, text: str) -> Decimal:
        """
        Extract amount from text.

        Patterns:
        - ₦2,000
        - N2000
        - 2000 naira
        - 2k
        """
        # Remove common words that might interfere
        text_clean = text.replace(",", "")

        # Try naira symbol patterns
        patterns = [
            r"₦\s*(\d+(?:\.\d{2})?)",  # ₦2000 or ₦2000.50
            r"N\s*(\d+(?:\.\d{2})?)",  # N2000
            r"(\d+(?:\.\d{2})?)\s*(?:naira|NGN)",  # 2000 naira
            r"(\d+)k",  # 2k (thousands)
            r"(\d+(?:\.\d{2})?)",  # Plain number as fallback
        ]

        for pattern in patterns:
            match = re.search(pattern, text_clean, re.IGNORECASE)
            if match:
                amount_str = match.group(1)
                amount = Decimal(amount_str)

                # Handle "k" notation (thousands)
                if "k" in text_clean.lower():
                    amount = amount * 1000

                return round(amount, 2)

        raise ValueError(f"Could not extract amount from text: {text}")

    def _extract_date(self, text: str) -> date | None:
        """
        Extract date from text.

        Patterns:
        - "today", "yesterday"
        - "Nov 10", "10 November"
        - "last week", "last month"
        """
        today = date.today()

        # Check for relative date keywords
        for keyword, days_offset in self.DATE_KEYWORDS.items():
            if keyword in text:
                return today + timedelta(days=days_offset)

        # Check for month + day patterns
        # Pattern: "nov 10", "10 november", "november 10"
        for month_name, month_num in self.MONTHS.items():
            # Try "Month Day" (Nov 10)
            pattern = rf"{month_name}\s+(\d{{1,2}})"
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                day = int(match.group(1))
                year = today.year
                # If month has passed, might be referring to last year
                if month_num < today.month or (month_num == today.month and day < today.day):
                    pass  # Same year
                elif month_num > today.month:
                    year = today.year - 1  # Previous year

                try:
                    return date(year, month_num, day)
                except ValueError:
                    pass

            # Try "Day Month" (10 Nov)
            pattern = rf"(\d{{1,2}})\s+{month_name}"
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                day = int(match.group(1))
                year = today.year
                try:
                    return date(year, month_num, day)
                except ValueError:
                    pass

        # Default to today if no date found
        return today

    def _categorize(self, text: str) -> str:
        """
        Categorize expense based on keywords in text.

        Returns category name or "other" if no match.
        """
        # Count keyword matches per category
        category_scores: dict[str, int] = {}

        for category, keywords in self.CATEGORY_KEYWORDS.items():
            score = 0
            for keyword in keywords:
                if keyword in text:
                    score += 1
            if score > 0:
                category_scores[category] = score

        # Return category with highest score
        if category_scores:
            return max(category_scores.items(), key=lambda x: x[1])[0]

        return "other"

    def _clean_description(self, text: str) -> str:
        """
        Extract and clean description from text.

        Remove amount symbols and common filler words.
        """
        # Remove amount patterns
        text = re.sub(r"₦\s*\d+[,\d]*(?:\.\d{2})?", "", text)
        text = re.sub(r"N\s*\d+[,\d]*(?:\.\d{2})?", "", text)
        text = re.sub(r"\d+k", "", text, flags=re.IGNORECASE)

        # Remove common prefixes
        prefixes = ["expense:", "expense", "spent", "paid for", "paid", "bought"]
        for prefix in prefixes:
            text = re.sub(rf"^{prefix}\s*", "", text, flags=re.IGNORECASE)

        # Remove date patterns
        text = re.sub(r"\b(today|yesterday|last week|last month)\b", "", text, flags=re.IGNORECASE)

        # Remove prepositions at start
        text = re.sub(r"^(for|on|at|in)\s+", "", text, flags=re.IGNORECASE)

        # Clean up whitespace
        text = " ".join(text.split())

        return text.strip()[:500]  # Limit to 500 chars

    def _extract_merchant(self, text: str) -> str | None:
        """
        Extract merchant/vendor name if mentioned.

        Patterns:
        - "from [merchant]"
        - "at [merchant]"
        """
        patterns = [
            r"(?:from|at)\s+([A-Z][a-zA-Z\s&]+)",  # "from MTN" or "at Shoprite"
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                merchant = match.group(1).strip()
                return merchant[:200]  # Limit to 200 chars

        return None
