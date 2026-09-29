from __future__ import annotations

import base64
import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from io import BytesIO
from typing import TYPE_CHECKING

import qrcode
from jinja2 import Environment, FileSystemLoader, select_autoescape
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.core.config import settings
from app.models.models import Invoice

if TYPE_CHECKING:  # pragma: no cover - for type hints only
    from app.models.tax_models import MonthlyTaxReport

try:
    from weasyprint import HTML  # type: ignore

    _WEASY_AVAILABLE = True
except Exception:  # noqa: BLE001
    _WEASY_AVAILABLE = False

logger = logging.getLogger(__name__)

_PDF_TIMEOUT = 30  # seconds — kill WeasyPrint if it hangs
_pdf_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="weasy")


def _weasy_render(html_str: str) -> bytes:
    """Run WeasyPrint in a thread so we can enforce a timeout."""
    return HTML(string=html_str).write_pdf()  # type: ignore


class PDFService:
    def __init__(self, s3_client):
        self.s3 = s3_client
        self.jinja = Environment(
            loader=FileSystemLoader("templates"),
            autoescape=select_autoescape(["html", "xml"]),
        )
        # Expose Python builtins the templates need
        self.jinja.globals["float"] = float

    def generate_invoice_pdf(
        self,
        invoice: Invoice,
        bank_details: dict | None = None,
        logo_url: str | None = None,
        user_plan: str = "free",  # Default to free plan
    ) -> str:
        """Generate PDF with bank transfer payment instructions and business logo.

        Args:
            invoice: Invoice model instance
            bank_details: Dict with bank_name, account_number, account_name
            logo_url: URL to business logo image
            user_plan: User's subscription plan (free/starter/pro/business)

        Returns:
            URL or path to generated PDF
        """
        customer_portal_url = self._build_customer_portal_url(invoice.invoice_id)
        qr_code_data = self._generate_qr_code(invoice.invoice_id)

        if settings.HTML_PDF_ENABLED and _WEASY_AVAILABLE:
            try:
                html_str = self._render_invoice_html(
                    invoice,
                    bank_details,
                    logo_url,
                    customer_portal_url,
                    qr_code_data,
                    user_plan,  # Pass plan to template
                )
                pdf_bytes = _pdf_pool.submit(_weasy_render, html_str).result(timeout=_PDF_TIMEOUT)
                key = f"invoices/{invoice.invoice_id}.pdf"
                url = self.s3.upload_bytes(pdf_bytes, key)
                logger.info("Uploaded HTML PDF for %s", invoice.invoice_id)
                return url
            except FuturesTimeoutError:
                logger.error("WeasyPrint timed out after %ds for %s", _PDF_TIMEOUT, invoice.invoice_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("HTML PDF generation failed (%s); falling back to ReportLab", e)
        # fallback path
        buffer = BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(40, 800, f"Invoice {invoice.invoice_id}")
        c.setFont("Helvetica", 12)
        c.drawString(40, 780, f"Customer: {invoice.customer.name}")
        _sym = "$" if getattr(invoice, "currency", "NGN") == "USD" else "₦"
        c.drawString(40, 760, f"Amount: {_sym}{invoice.amount:,.2f}")

        # Add bank transfer details
        if bank_details:
            y = 720
            c.setFont("Helvetica-Bold", 12)
            c.drawString(40, y, "Payment Details (Bank Transfer):")
            c.setFont("Helvetica", 10)
            y -= 20
            if bank_details.get("bank_name"):
                c.drawString(50, y, f"Bank: {bank_details['bank_name']}")
                y -= 15
            if bank_details.get("account_number"):
                c.drawString(50, y, f"Account Number: {bank_details['account_number']}")
                y -= 15
            if bank_details.get("account_name"):
                c.drawString(50, y, f"Account Name: {bank_details['account_name']}")
                y -= 20
        else:
            y = 720

        c.setFont("Helvetica", 10)
        c.drawString(40, y, "After transferring, let us know here:")
        y -= 15
        c.setFillColorRGB(0, 0, 0.6)
        c.drawString(40, y, customer_portal_url)
        c.setFillColorRGB(0, 0, 0)
        y -= 25
        # Invoice lines
        c.setFont("Helvetica-Bold", 12)
        y -= 10
        c.drawString(40, y, "Items:")
        c.setFont("Helvetica", 10)
        y -= 20
        for line in invoice.lines:
            c.drawString(50, y, f"- {line.description} x{line.quantity} @ {_sym}{line.unit_price:,.2f}")
            y -= 20
        c.showPage()
        c.save()
        pdf_bytes = buffer.getvalue()
        key = f"invoices/{invoice.invoice_id}.pdf"
        url = self.s3.upload_bytes(pdf_bytes, key)
        logger.info("Uploaded fallback PDF for %s", invoice.invoice_id)
        return url

    def generate_receipt_pdf(self, invoice: Invoice) -> str:
        """Generate a payment receipt PDF with PAID watermark.

        Uses HTML template if available (receipt.html), otherwise
        falls back to a minimal ReportLab layout with diagonal PAID watermark.
        """
        paid_at_display = None
        if getattr(invoice, "paid_at", None):
            try:
                paid_at_display = invoice.paid_at.strftime("%B %d, %Y %H:%M UTC")
            except Exception:  # noqa: BLE001
                paid_at_display = str(invoice.paid_at)
        else:
            paid_at_display = "(time not recorded)"

        # Generate QR code for receipt verification
        qr_code_data = self._generate_qr_code(invoice.invoice_id)

        # Get business info (logo and name) from issuer
        logo_url = None
        business_name = None
        if hasattr(invoice, "issuer") and invoice.issuer:
            business_name = getattr(invoice.issuer, "business_name", None)
            # Generate fresh presigned URL for logo if stored
            stored_logo_url = getattr(invoice.issuer, "logo_url", None)
            if stored_logo_url:
                logo_key = self.s3.extract_key_from_url(stored_logo_url)
                if logo_key:
                    logo_url = self.s3.get_presigned_url(logo_key, expires_in=3600)
                if not logo_url:
                    # Fallback to stored URL if key extraction fails
                    logo_url = stored_logo_url

        if settings.HTML_PDF_ENABLED and _WEASY_AVAILABLE:
            try:
                # If a dedicated receipt template exists use it; otherwise reuse invoice.html
                template_name = "receipt.html"
                try:
                    template = self.jinja.get_template(template_name)
                except Exception:  # noqa: BLE001
                    template = self.jinja.get_template("invoice.html")
                watermark_text = "PAID"  # force PAID watermark on receipt

                # Get creator name if available
                created_by_name = None
                if hasattr(invoice, "created_by") and invoice.created_by:
                    created_by_name = invoice.created_by.name

                # Get confirmer name if available
                confirmed_by_name = None
                if hasattr(invoice, "status_updated_by") and invoice.status_updated_by:
                    confirmed_by_name = invoice.status_updated_by.name

                # Determine currency symbol from invoice
                currency = getattr(invoice, "currency", "NGN") or "NGN"
                currency_symbol = "$" if currency == "USD" else "₦"

                html_str = template.render(
                    invoice=invoice,
                    bank_details=None,
                    logo_url=logo_url,
                    business_name=business_name,
                    customer_portal_url=None,
                    qr_code=qr_code_data,
                    watermark_text=watermark_text,
                    paid_at_display=paid_at_display,
                    is_receipt=True,
                    created_by_name=created_by_name,
                    confirmed_by_name=confirmed_by_name,
                    currency_symbol=currency_symbol,
                )
                pdf_bytes = _pdf_pool.submit(_weasy_render, html_str).result(timeout=_PDF_TIMEOUT)
                key = f"receipts/{invoice.invoice_id}.pdf"
                url = self.s3.upload_bytes(pdf_bytes, key)
                logger.info("Uploaded receipt HTML PDF for %s", invoice.invoice_id)
                return url
            except FuturesTimeoutError:
                logger.error("WeasyPrint receipt timed out after %ds for %s", _PDF_TIMEOUT, invoice.invoice_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("Receipt HTML generation failed (%s); using fallback", e)

        # Fallback ReportLab rendering
        buf = BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        c.setFont("Helvetica-Bold", 18)
        c.drawString(40, 800, f"Payment Receipt {invoice.invoice_id}")
        c.setFont("Helvetica", 11)
        y = 775
        c.drawString(40, y, f"Customer: {getattr(invoice.customer, 'name', 'Customer')}")
        y -= 15
        _sym = "$" if getattr(invoice, "currency", "NGN") == "USD" else "₦"
        c.drawString(40, y, f"Amount Paid: {_sym}{invoice.amount:,.2f}")
        y -= 15
        c.drawString(40, y, "Status: PAID")
        y -= 15
        c.drawString(40, y, f"Payment Date: {paid_at_display}")
        y -= 15

        # Add creator and confirmer info
        if hasattr(invoice, "created_by") and invoice.created_by:
            c.drawString(40, y, f"Invoice Created by: {invoice.created_by.name}")
            y -= 15
        if hasattr(invoice, "status_updated_by") and invoice.status_updated_by:
            c.drawString(40, y, f"Payment Confirmed by: {invoice.status_updated_by.name}")
            y -= 15

        y -= 10
        c.setFont("Helvetica", 9)
        c.drawString(
            40,
            y,
            "Thank you for your payment. This receipt confirms full settlement of this invoice.",
        )
        # PAID watermark
        c.saveState()
        c.setFont("Helvetica-Bold", 70)
        c.setFillColorRGB(0.0, 0.6, 0.2)  # solid color (ReportLab lacks alpha pre 3.0)
        c.translate(300, 400)
        c.rotate(30)
        c.drawString(-160, 0, "PAID")
        c.restoreState()
        c.showPage()
        c.save()
        pdf_bytes = buf.getvalue()
        key = f"receipts/{invoice.invoice_id}.pdf"
        url = self.s3.upload_bytes(pdf_bytes, key)
        logger.info("Uploaded fallback receipt PDF for %s", invoice.invoice_id)
        return url

    def _render_invoice_html(
        self,
        invoice: Invoice,
        bank_details: dict | None,
        logo_url: str | None = None,
        customer_portal_url: str | None = None,
        qr_code_data: str | None = None,
        user_plan: str = "free",
    ) -> str:
        """Render invoice HTML template with bank transfer details and business logo."""
        # Use expense template for expense invoices
        template_name = "expense_invoice.html" if invoice.invoice_type == "expense" else "invoice.html"
        template = self.jinja.get_template(template_name)
        watermark_text = settings.PDF_WATERMARK_TEXT if getattr(settings, "PDF_WATERMARK_ENABLED", False) else None

        # Fetch and encode receipt image for expense invoices
        receipt_data_url = None
        if invoice.invoice_type == "expense" and invoice.receipt_url:
            receipt_data_url = self._fetch_receipt_as_data_url(invoice.receipt_url)

        # Check if user is eligible for VAT tracking (BUSINESS plan only)
        is_vat_eligible = user_plan.lower() == "business"

        # Get creator name if available
        created_by_name = None
        if hasattr(invoice, "created_by") and invoice.created_by:
            created_by_name = invoice.created_by.name

        # Get business name from issuer
        business_name = None
        if hasattr(invoice, "issuer") and invoice.issuer:
            business_name = getattr(invoice.issuer, "business_name", None)

        # Online payments (card/transfer via the pay portal) available? When on,
        # the buyer pays through the portal link, so the invoice must NOT show a
        # "bank details missing — contact the business" warning.
        issuer = getattr(invoice, "issuer", None)
        online_payments_enabled = bool(issuer is not None and getattr(issuer, "online_payments_active", False))

        # Determine currency symbol from invoice
        currency = getattr(invoice, "currency", "NGN") or "NGN"
        currency_symbol = "$" if currency == "USD" else "₦"

        return template.render(
            invoice=invoice,
            bank_details=bank_details,
            logo_url=logo_url,
            business_name=business_name,
            customer_portal_url=customer_portal_url,
            online_payments_enabled=online_payments_enabled,
            qr_code=qr_code_data,
            watermark_text=watermark_text,
            receipt_data_url=receipt_data_url,
            user_plan=user_plan.lower(),
            is_vat_eligible=is_vat_eligible,
            created_by_name=created_by_name,
            currency_symbol=currency_symbol,
        )

    # ---------------- Monthly Tax Report PDF -----------------
    def generate_monthly_tax_report_pdf(
        self,
        report: MonthlyTaxReport,  # forward reference resolved under TYPE_CHECKING
        basis: str,
        total_revenue: float = 0,
        total_expenses: float = 0,
    ) -> str:
        """Generate monthly tax compliance PDF (levy + VAT breakdown).

        Uses HTML template if WeasyPrint enabled; else simple ReportLab fallback.
        """
        from app.models.tax_models import MonthlyTaxReport  # local import to avoid circular

        assert isinstance(report, MonthlyTaxReport)
        cogs = float(report.cogs_amount or 0)
        # Attempt HTML path first
        if settings.HTML_PDF_ENABLED and _WEASY_AVAILABLE:
            try:
                template = self.jinja.get_template("monthly_tax_report.html")
                watermark_text = (
                    settings.PDF_WATERMARK_TEXT if getattr(settings, "PDF_WATERMARK_ENABLED", False) else None
                )
                html_str = template.render(
                    report=report,
                    basis=basis,
                    watermark_text=watermark_text,
                    total_revenue=total_revenue,
                    total_expenses=total_expenses,
                    cogs_amount=cogs,
                )
                from weasyprint import HTML  # type: ignore

                pdf_bytes = HTML(string=html_str).write_pdf()  # type: ignore
                key = f"tax-reports/{report.user_id}/{report.year}-{report.month}.pdf"
                return self.s3.upload_bytes(pdf_bytes, key)
            except Exception as e:  # noqa: BLE001
                logger.warning("Monthly tax report HTML generation failed (%s); using fallback", e)
        # Fallback ReportLab rendering — SuoOps brand colors
        # Evergreen #0B3318, Jade #14B56A, Teal #0F766E, Mint #E8F5EC
        buf = BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        page_w, page_h = A4  # 595, 842

        # ── Header bar (evergreen background) ──
        c.setFillColorRGB(0.043, 0.2, 0.094)  # #0B3318
        c.rect(0, page_h - 60, page_w, 60, fill=True, stroke=False)
        c.setFillColorRGB(0.078, 0.71, 0.416)  # #14B56A for "Suo"
        c.setFont("Helvetica-Bold", 18)
        c.drawString(40, page_h - 38, "Suo")
        c.setFillColorRGB(1, 1, 1)
        c.drawString(72, page_h - 38, "Ops")
        # Title
        month_part = f"-{report.month:02d}" if report.month else ""
        title = f" — Tax Report {report.year}{month_part}"
        if getattr(report, "period_type", "month") != "month":
            title = f" — Tax Report {report.start_date} to {report.end_date}"
        c.setFont("Helvetica", 14)
        c.drawString(100, page_h - 38, title)
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.91, 0.96, 0.925)  # #E8F5EC
        c.drawString(40, page_h - 54, f"Internal Compliance Report  |  NTA 2025  |  Basis: {basis}")

        # ── Period meta bar (mint bg, jade left border) ──
        y_meta = page_h - 80
        c.setFillColorRGB(0.91, 0.96, 0.925)  # mint
        c.rect(40, y_meta - 4, page_w - 80, 20, fill=True, stroke=False)
        c.setFillColorRGB(0.078, 0.71, 0.416)  # jade accent
        c.rect(40, y_meta - 4, 3, 20, fill=True, stroke=False)
        c.setFillColorRGB(0.059, 0.463, 0.431)  # teal text
        c.setFont("Helvetica", 9)
        period_str = (
            f"{report.start_date} to {report.end_date}"
            if report.start_date and report.end_date
            else f"{report.year}-{report.month:02d}"
            if report.month
            else str(report.year)
        )
        c.drawString(50, y_meta + 2, f"Period: {period_str}   |   Generated: {getattr(report, 'generated_at', '')}")

        # ── Section helper ──
        def draw_section_title(canvas_obj, ypos, text):
            canvas_obj.setFillColorRGB(0.043, 0.2, 0.094)  # evergreen
            canvas_obj.setFont("Helvetica-Bold", 12)
            canvas_obj.drawString(40, ypos, text)
            # jade underline
            tw = canvas_obj.stringWidth(text, "Helvetica-Bold", 12)
            canvas_obj.setStrokeColorRGB(0.078, 0.71, 0.416)
            canvas_obj.setLineWidth(1.5)
            canvas_obj.line(40, ypos - 3, 40 + tw + 4, ypos - 3)
            canvas_obj.setStrokeColorRGB(0, 0, 0)
            canvas_obj.setLineWidth(1)
            return ypos - 20

        # ── Income Summary ──
        y = draw_section_title(c, y_meta - 30, "Income Summary")
        c.setFont("Helvetica", 10)
        c.setFillColorRGB(0.059, 0.118, 0.09)  # charcoal
        income_rows = [
            ("Total Revenue", f"₦{total_revenue:,.2f}"),
            ("Total Expenses", f"₦{total_expenses:,.2f}"),
            ("Cost of Goods Sold (COGS)", f"₦{cogs:,.2f}"),
        ]
        for label, val in income_rows:
            c.drawString(50, y, label)
            c.drawRightString(page_w - 60, y, val)
            y -= 16
        # Assessable Profit row (bold, mint bg)
        c.setFillColorRGB(0.91, 0.96, 0.925)
        c.rect(42, y - 4, page_w - 102, 16, fill=True, stroke=False)
        c.setFillColorRGB(0.043, 0.2, 0.094)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(50, y, "Assessable Profit")
        c.drawRightString(page_w - 60, y, f"₦{float(report.assessable_profit or 0):,.2f}")
        y -= 26

        # ── Tax & Levy ──
        y = draw_section_title(c, y, "Tax & Levy")
        c.setFont("Helvetica", 10)
        c.setFillColorRGB(0.059, 0.118, 0.09)
        c.drawString(50, y, "Development Levy")
        c.drawRightString(page_w - 60, y, f"₦{float(report.levy_amount or 0):,.2f}")
        y -= 26

        # ── VAT Summary ──
        y = draw_section_title(c, y, "VAT Summary")
        c.setFont("Helvetica-Oblique", 8)
        c.setFillColorRGB(0.059, 0.463, 0.431)  # teal
        c.drawString(
            50,
            y,
            "Internal VAT summary based on invoices created in SuoOps. Review with your accountant before filing.",
        )
        y -= 14
        c.setFont("Helvetica", 10)
        c.setFillColorRGB(0.059, 0.118, 0.09)
        vat_rows = [
            ("VAT Collected", f"₦{float(report.vat_collected or 0):,.2f}"),
            ("Taxable Sales", f"₦{float(report.taxable_sales or 0):,.2f}"),
            ("Zero-rated Sales", f"₦{float(report.zero_rated_sales or 0):,.2f}"),
            ("Exempt Sales", f"₦{float(report.exempt_sales or 0):,.2f}"),
        ]
        for label, val in vat_rows:
            c.drawString(50, y, label)
            c.drawRightString(page_w - 60, y, val)
            y -= 16

        # ── Watermark ──
        if getattr(settings, "PDF_WATERMARK_ENABLED", False):
            c.saveState()
            c.setFont("Helvetica", 60)
            c.setFillColorRGB(0.043, 0.2, 0.094, 0.04)
            c.translate(300, 400)
            c.rotate(30)
            c.drawString(-200, 0, settings.PDF_WATERMARK_TEXT[:30])
            c.restoreState()

        # ── Footer ──
        c.setFillColorRGB(0.059, 0.463, 0.431)  # teal
        c.setFont("Helvetica-Oblique", 7.5)
        c.drawString(
            40,
            40,
            "Disclaimer: Internal VAT & tax summary. VAT reflects what the business "
            "charged — SuoOps does not assume obligations or decide exemptions.",
        )
        c.drawString(
            40,
            30,
            "Review with your accountant before filing with FIRS.",
        )

        # ════════════════ Page 2: Filing Guidance ════════════════
        c.showPage()
        # Header bar
        c.setFillColorRGB(0.043, 0.2, 0.094)
        c.rect(0, page_h - 50, page_w, 50, fill=True, stroke=False)
        c.setFillColorRGB(0.078, 0.71, 0.416)
        c.setFont("Helvetica-Bold", 15)
        c.drawString(40, page_h - 34, "Suo")
        c.setFillColorRGB(1, 1, 1)
        c.drawString(68, page_h - 34, "Ops — Filing Guidance")

        y = page_h - 74
        c.setFillColorRGB(0.043, 0.2, 0.094)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(40, y, "Additional Details to Be Completed by the Business Owner")
        y -= 16
        c.setFillColorRGB(0.059, 0.463, 0.431)
        c.setFont("Helvetica", 8.5)
        c.drawString(40, y, "The sections below are required when submitting to your accountant or FIRS.")
        y -= 12
        c.drawString(40, y, "SuoOps does not generate, verify, or file any of this information.")
        y -= 22

        # 1. Business Identification
        y = draw_section_title(c, y, "1. Business Identification")
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.059, 0.118, 0.09)
        c.drawString(40, y, "These details are required by your accountant or FIRS.")
        y -= 18
        id_fields = [
            "Registered Business Name:",
            "Business Type:  [ ] Sole Proprietor  [ ] Partnership  [ ] Limited Company",
            "CAC Registration Number:",
            "Tax Identification Number (TIN):",
            "FIRS Tax Office / Jurisdiction:",
            "Business Address:",
            "Contact Email / Phone:",
        ]
        for field in id_fields:
            c.drawString(50, y, field)
            if not field.startswith("Business Type"):
                c.setStrokeColorRGB(0.059, 0.463, 0.431)  # teal lines
                c.line(280, y - 2, 540, y - 2)
            y -= 18

        # 2. VAT Status Declaration
        y -= 8
        y = draw_section_title(c, y, "2. VAT Status Declaration")
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.059, 0.118, 0.09)
        c.drawString(40, y, "Tick the option that applies to your business.")
        y -= 16
        for opt in ["[ ] VAT Registered", "[ ] VAT Exempt", "[ ] Below VAT Threshold"]:
            c.drawString(50, y, opt)
            y -= 14
        y -= 2
        c.drawString(50, y, "If VAT collected is N0.00, state reason:")
        c.setStrokeColorRGB(0.059, 0.463, 0.431)
        c.line(260, y - 2, 540, y - 2)
        y -= 22

        # 3. Monthly Filings
        y = draw_section_title(c, y, "3. Monthly Filings")
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.059, 0.118, 0.09)
        c.drawString(40, y, "This report is a summary. Confirm separately with your accountant:")
        y -= 16
        for item in [
            "[ ] Monthly VAT Returns (Jan-Dec)",
            "[ ] PAYE Returns (if you have employees)",
            "[ ] Withholding Tax (if applicable)",
        ]:
            c.drawString(50, y, item)
            y -= 14

        # 4. Declaration
        y -= 10
        y = draw_section_title(c, y, "4. Declaration")
        # Declaration box (mint bg, teal border)
        box_h = 100
        c.setStrokeColorRGB(0.059, 0.463, 0.431)
        c.setFillColorRGB(0.957, 0.98, 0.965)  # #f4faf6
        c.roundRect(40, y - box_h + 10, page_w - 80, box_h, 3, fill=True, stroke=True)
        c.setFillColorRGB(0.059, 0.118, 0.09)
        c.setFont("Helvetica", 9)
        c.drawString(50, y - 4, "I declare that the information in this report is true and complete")
        c.drawString(50, y - 16, "to the best of my knowledge.")
        y -= 36
        for label in ["Name:", "Designation:", "Signature:", "Date:"]:
            c.drawString(50, y, label)
            c.setStrokeColorRGB(0.059, 0.463, 0.431)
            c.line(140, y - 2, 400, y - 2)
            y -= 18

        # Footer
        c.setFillColorRGB(0.059, 0.463, 0.431)
        c.setFont("Helvetica-Oblique", 7.5)
        c.drawString(
            40,
            40,
            "Disclaimer: Internal VAT & tax summary. VAT reflects what the business "
            "charged — SuoOps does not assume obligations or decide exemptions.",
        )
        c.drawString(
            40,
            30,
            "Review with your accountant before filing with FIRS.",
        )
        c.showPage()
        c.save()
        pdf_bytes = buf.getvalue()
        key = f"tax-reports/{report.user_id}/{report.year}-{report.month}.pdf"
        return self.s3.upload_bytes(pdf_bytes, key)

    def _build_customer_portal_url(self, invoice_id: str) -> str:
        base = settings.FRONTEND_URL.rstrip("/")
        return f"{base}/pay/{invoice_id}"

    def _generate_qr_code(self, invoice_id: str) -> str:
        """Generate QR code as base64 data URI for verification URL.

        Args:
            invoice_id: Invoice ID to encode in QR code

        Returns:
            Base64 encoded QR code image as data URI
        """
        # QR points at the branded verification PAGE (not the raw API), so a scan
        # opens a human-readable "Verified by SuoOps" card, not JSON.
        frontend = settings.FRONTEND_URL.rstrip("/")
        verify_url = f"{frontend}/verify/{invoice_id}"

        # Create QR code
        qr = qrcode.QRCode(
            version=1,  # Size of QR code (1-40, 1 is smallest)
            error_correction=qrcode.constants.ERROR_CORRECT_M,  # ~15% error correction
            box_size=10,  # Size of each box in pixels
            border=2,  # Border size in boxes
        )
        qr.add_data(verify_url)
        qr.make(fit=True)

        # Generate image
        img = qr.make_image(fill_color="black", back_color="white")

        # Convert to base64 data URI
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        img_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

        return f"data:image/png;base64,{img_base64}"

    def _fetch_receipt_as_data_url(self, receipt_url: str) -> str | None:
        """Fetch receipt image from S3 and convert to base64 data URL.

        Args:
            receipt_url: S3 URL of the receipt image

        Returns:
            Base64 encoded image as data URI, or None if fetch fails
        """
        try:
            import mimetypes
            from urllib.parse import urlparse

            import requests

            # SSRF protection: only allow HTTPS URLs from trusted domains
            parsed = urlparse(receipt_url)
            if parsed.scheme not in ("https", "http"):
                logger.warning("Blocked non-HTTP receipt URL scheme: %s", parsed.scheme)
                return None
            allowed_hosts = {
                "s3.amazonaws.com",
                "whatsinvoice.s3.amazonaws.com",
                "whatsinvoice.s3.us-east-1.amazonaws.com",
            }
            host = (parsed.hostname or "").lower()
            if not any(host == h or host.endswith(f".{h}") for h in allowed_hosts):
                logger.warning("Blocked receipt URL from untrusted host: %s", host)
                return None

            # Fetch the image from S3 with timeout
            response = requests.get(receipt_url, timeout=15)
            response.raise_for_status()
            image_data = response.content

            # Determine MIME type from URL or response headers
            mime_type = response.headers.get("Content-Type")
            if not mime_type:
                mime_type = mimetypes.guess_type(receipt_url)[0]
            if not mime_type:
                # Default to jpeg if unknown
                mime_type = "image/jpeg"

            # Convert to base64
            img_base64 = base64.b64encode(image_data).decode("utf-8")
            logger.info(
                "Successfully fetched and encoded receipt image from %s (size: %d bytes)",
                receipt_url,
                len(image_data),
            )
            return f"data:{mime_type};base64,{img_base64}"

        except Exception as e:  # noqa: BLE001
            logger.error("Failed to fetch receipt image from %s: %s", receipt_url, e)
            return None
