#!/usr/bin/env python3
"""
Create/Update WhatsApp Message Template for Invoice with Payment Details

This script creates an approved WhatsApp message template for notifying
customers about new invoices WITH bank payment details included.

Usage:
    python scripts/create_whatsapp_payment_template.py

Requirements:
    - WHATSAPP_API_KEY environment variable (Meta API token)
    - WHATSAPP_BUSINESS_ACCOUNT_ID
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
TEMPLATE_NAME = "invoice_with_payment"
TEMPLATE_LANGUAGE = "en"
TEMPLATE_CATEGORY = "UTILITY"  # UTILITY for transactional messages

# Template body with parameters:
# {{1}} = Customer name
# {{2}} = Invoice ID
# {{3}} = Amount (e.g., ₦50,000.00)
# {{4}} = Items/description
# {{5}} = Bank name
# {{6}} = Account number
# {{7}} = Account name
# {{8}} = Payment link
TEMPLATE_BODY = """Hi {{1}}, you have a new invoice.

📄 Invoice: {{2}}
💰 Amount: {{3}}
📋 Items: {{4}}

💳 Bank Transfer:
Bank: {{5}}
Account: {{6}}
Name: {{7}}

🔗 Pay online at {{8}} to complete your payment.

💡 Reply "OK" if you don't receive the invoice PDF."""


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
                "example": {
                    "body_text": [
                        [
                            "John Doe",
                            "INV-2024-001",
                            "₦50,000.00",
                            "Consulting services",
                            "Access Bank",
                            "0123456789",
                            "Business Name Ltd",
                            "https://suoops.com/pay/INV-2024-001",
                        ]
                    ]
                },
            }
        ],
    }

    print(f"📤 Creating WhatsApp template '{TEMPLATE_NAME}'...")
    print(f"   WABA ID: {WABA_ID}")
    print(f"   Category: {TEMPLATE_CATEGORY}")
    print(f"   Language: {TEMPLATE_LANGUAGE}")
    print("   Parameters: 8 (name, invoice_id, amount, items, bank, account, account_name, link)")
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
        print(f"   3. Once approved, add to Render: WHATSAPP_TEMPLATE_INVOICE_PAYMENT={TEMPLATE_NAME}")
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
            elif "duplicate" in error_msg.lower():
                print()
                print("ℹ️  Template with this name already exists.")
                print("   To update it, you need to delete the old one first or use a different name.")
                check_template_status()
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
        "fields": "name,status,language,category,id",
        "limit": 50,
    }

    response = requests.get(url, headers=headers, params=params)

    if response.status_code == 200:
        data = response.json()
        templates = data.get("data", [])

        print()
        print("📋 Existing WhatsApp Templates:")
        print("-" * 70)

        payment_template = None
        for t in templates:
            status_emoji = "✅" if t.get("status") == "APPROVED" else "⏳" if t.get("status") == "PENDING" else "❌"
            print(f"   {status_emoji} {t.get('name')} ({t.get('language')}) - {t.get('status')} [ID: {t.get('id')}]")

            if t.get("name") == TEMPLATE_NAME:
                payment_template = t

        print()

        if payment_template:
            status = payment_template.get("status")
            if status == "APPROVED":
                print(f"✅ '{TEMPLATE_NAME}' is APPROVED!")
                print(f"   Add to Render: WHATSAPP_TEMPLATE_INVOICE_PAYMENT={TEMPLATE_NAME}")
            elif status == "PENDING":
                print(f"⏳ '{TEMPLATE_NAME}' is pending approval...")
                print("   Check back in 24-48 hours")
            elif status == "REJECTED":
                print(f"❌ '{TEMPLATE_NAME}' was REJECTED")
                print("   Check Meta Business Manager for rejection reason")
                print(f"   You may need to delete (ID: {payment_template.get('id')}) and recreate with adjustments")
            else:
                print(f"❌ '{TEMPLATE_NAME}' status: {status}")
        else:
            print(f"ℹ️  Template '{TEMPLATE_NAME}' not found")
    else:
        print(f"❌ Failed to fetch templates: {response.status_code}")
        print(f"   {response.text}")


def delete_template(template_name=None):
    """Delete a template by name."""
    if not template_name:
        template_name = TEMPLATE_NAME

    # First, get the template ID
    url = f"https://graph.facebook.com/v18.0/{WABA_ID}/message_templates"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    params = {"name": template_name}

    response = requests.get(url, headers=headers, params=params)

    if response.status_code == 200:
        data = response.json()
        templates = data.get("data", [])

        if templates:
            template_id = templates[0].get("id")
            template_name_found = templates[0].get("name")

            # Delete the template
            delete_url = f"https://graph.facebook.com/v18.0/{WABA_ID}/message_templates"
            delete_params = {"name": template_name_found}

            print(f"🗑️  Deleting template '{template_name_found}' (ID: {template_id})...")
            delete_response = requests.delete(delete_url, headers=headers, params=delete_params)

            if delete_response.status_code == 200:
                print("✅ Template deleted successfully!")
                return True
            else:
                print(f"❌ Failed to delete template: {delete_response.status_code}")
                print(f"   {delete_response.text}")
                return False
        else:
            print(f"❌ Template '{template_name}' not found")
            return False
    else:
        print(f"❌ Failed to fetch template: {response.status_code}")
        return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Manage WhatsApp Invoice Payment Template")
    parser.add_argument("--list", action="store_true", help="List existing templates")
    parser.add_argument("--create", action="store_true", help="Create payment template")
    parser.add_argument("--delete", action="store_true", help="Delete payment template")
    parser.add_argument("--recreate", action="store_true", help="Delete and recreate template")

    args = parser.parse_args()

    if args.list:
        check_template_status()
    elif args.delete:
        delete_template()
    elif args.recreate:
        if delete_template():
            print()
            create_template()
    elif args.create:
        create_template()
    else:
        # Default: try to create, show status if exists
        create_template()
