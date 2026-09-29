#!/usr/bin/env python3
"""
Create WhatsApp Message Template for Invoice Notifications

This script creates an approved WhatsApp message template for notifying
first-time customers about new invoices.

Usage:
    python scripts/create_whatsapp_invoice_template.py

Requirements:
    - WHATSAPP_API_KEY environment variable (Meta API token)
    - WHATSAPP_BUSINESS_ACCOUNT_ID: 713163545130337
"""

import os
import sys

import requests

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

# Configuration - Use production values from Render
WABA_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "1557030862208862")
ACCESS_TOKEN = os.getenv("WHATSAPP_API_KEY")

if not ACCESS_TOKEN:
    print("❌ Error: WHATSAPP_API_KEY environment variable not set")
    sys.exit(1)

# Template configuration
TEMPLATE_NAME = "invoice_notification"
TEMPLATE_LANGUAGE = "en"
TEMPLATE_CATEGORY = "UTILITY"  # UTILITY, MARKETING, or AUTHENTICATION

# Template body with parameters:
# {{1}} = Customer name
# {{2}} = Invoice ID
# {{3}} = Amount (e.g., ₦50,000.00)
# {{4}} = Items/description
TEMPLATE_BODY = """Hi {{1}}, you have a new invoice.

📄 Invoice: {{2}}
💰 Amount: {{3}}
📋 Items: {{4}}

Reply 'Hi' to view payment details and complete your payment."""


def create_template():
    """Create the WhatsApp message template via Graph API."""
    url = f"https://graph.facebook.com/v18.0/{WABA_ID}/message_templates"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "name": TEMPLATE_NAME,
        "language": TEMPLATE_LANGUAGE,
        "category": TEMPLATE_CATEGORY,
        "components": [
            {
                "type": "BODY",
                "text": TEMPLATE_BODY,
                "example": {"body_text": [["John Doe", "INV-2024-001", "₦50,000.00", "Consulting services"]]},
            }
        ],
    }

    print(f"📤 Creating WhatsApp template '{TEMPLATE_NAME}'...")
    print(f"   WABA ID: {WABA_ID}")
    print(f"   Category: {TEMPLATE_CATEGORY}")
    print(f"   Language: {TEMPLATE_LANGUAGE}")
    print()

    response = requests.post(url, headers=headers, json=payload)

    if response.status_code == 200:
        data = response.json()
        print("✅ Template created successfully!")
        print(f"   Template ID: {data.get('id')}")
        print(f"   Status: {data.get('status', 'PENDING')}")
        print()
        print("📝 Next steps:")
        print("   1. Template is pending Meta approval (usually 24-48 hours)")
        print("   2. Check status at: Meta Business Manager → WhatsApp → Message Templates")
        print(f"   3. Once approved, add to Render: WHATSAPP_TEMPLATE_INVOICE={TEMPLATE_NAME}")
        return True
    else:
        print("❌ Failed to create template")
        print(f"   Status: {response.status_code}")
        print(f"   Response: {response.text}")

        # Handle common errors
        try:
            error_data = response.json()
            error_msg = error_data.get("error", {}).get("message", "")
            error_code = error_data.get("error", {}).get("code", 0)

            if error_code == 100 and "already exists" in error_msg.lower():
                print()
                print("ℹ️  Template already exists. Checking status...")
                check_template_status()
            elif error_code == 190:
                print()
                print("⚠️  Access token may be expired. Please refresh your Meta API token.")
        except Exception:
            pass

        return False


def check_template_status():
    """Check the status of existing templates."""
    url = f"https://graph.facebook.com/v18.0/{WABA_ID}/message_templates"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
    }

    params = {
        "fields": "name,status,language,category",
        "limit": 50,
    }

    response = requests.get(url, headers=headers, params=params)

    if response.status_code == 200:
        data = response.json()
        templates = data.get("data", [])

        print()
        print("📋 Existing WhatsApp Templates:")
        print("-" * 60)

        invoice_template = None
        for t in templates:
            status_emoji = "✅" if t.get("status") == "APPROVED" else "⏳" if t.get("status") == "PENDING" else "❌"
            print(f"   {status_emoji} {t.get('name')} ({t.get('language')}) - {t.get('status')}")

            if t.get("name") == TEMPLATE_NAME:
                invoice_template = t

        print()

        if invoice_template:
            status = invoice_template.get("status")
            if status == "APPROVED":
                print(f"✅ '{TEMPLATE_NAME}' is APPROVED!")
                print(f"   Add to Render: WHATSAPP_TEMPLATE_INVOICE={TEMPLATE_NAME}")
            elif status == "PENDING":
                print(f"⏳ '{TEMPLATE_NAME}' is pending approval...")
                print("   Check back in 24-48 hours")
            else:
                print(f"❌ '{TEMPLATE_NAME}' status: {status}")
                print("   You may need to delete and recreate the template")
        else:
            print(f"ℹ️  Template '{TEMPLATE_NAME}' not found")
    else:
        print(f"❌ Failed to fetch templates: {response.status_code}")
        print(f"   {response.text}")


def list_templates():
    """List all WhatsApp message templates."""
    check_template_status()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Manage WhatsApp Message Templates")
    parser.add_argument("--list", action="store_true", help="List existing templates")
    parser.add_argument("--create", action="store_true", help="Create invoice template")

    args = parser.parse_args()

    if args.list:
        list_templates()
    elif args.create:
        create_template()
    else:
        # Default: try to create, show status if exists
        create_template()
