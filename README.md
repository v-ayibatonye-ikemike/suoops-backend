# SuoOps

WhatsApp-first invoicing platform for micro and informal businesses.

**Live at**: https://suoops.com  
**API**: https://api.suoops.com

## Stack
FastAPI, SQLAlchemy, PostgreSQL, Redis (tasks), Celery, ReportLab (PDF), Paystack (payment abstraction), S3-compatible storage.

## Quick Start
```bash
poetry install
cp .env.example .env
poetry run uvicorn app.api.main:app --reload
```

> Deployments use `requirements.txt` + `.python-version`. The repository intentionally omits `poetry.lock` so the build environment doesn't detect multiple package managers—generate it locally if you need a lockfile, but leave it untracked.

### Frontend
```bash
cd frontend
npm install
npm run dev
```

To refresh strongly typed API bindings after backend schema updates:
1. Generate the latest schema with `npm run openapi` (writes to `src/api/types.generated.ts`).
2. Copy any changed shapes that the app uses into the curated `src/api/types.ts`, keeping the file under ~400 LOC.
3. Remove the generated file before committing, or leave it untracked per team preference.

## Directory Structure
```
app/
  api/            # FastAPI entry + routers
  bot/            # WhatsApp adapter + NLP
  core/           # config & logging
  db/             # base + session
  models/         # SQLAlchemy models & Pydantic schemas
  services/       # domain services (invoice, payment, pdf, ocr, notify)
  storage/        # S3 client abstraction
  utils/          # helpers (ids, currency, validators)
  workers/        # background tasks definitions
templates/        # HTML templates for PDF generation
tests/            # pytest suite
```

## High-Level Flow

User sends WhatsApp → webhook → NLP parse → InvoiceService → Payment link → PDF → WhatsApp send.

### Inventory cost reporting

Sale stock movements snapshot the product's acquisition cost, not its selling
price. Later product cost changes do not rewrite historical COGS. Explicit
zero-cost stock adjustments remain zero; an omitted adjustment cost uses the
product's current cost. Missing product costs remain unknown and contribute zero
to the existing COGS aggregate, so businesses should enter acquisition costs for
accurate profit reporting.

Existing movements recorded with selling prices are not automatically rewritten:
their original acquisition costs must be reconciled against purchasing records.

Sales, purchases, and adjustments refresh and lock the product before changing
stock, so cached session values cannot overwrite a newer balance. Replaying a
sale for the same invoice line returns its original movement instead of deducting
stock again. Sales without an invoice-line reference remain separate movements.

### Invoice entry and transfer confirmation

The web invoice form preserves each priced line, using "Item" when its description
is blank. Quantities must be positive whole numbers, prices must be positive and
finite, and the submitted total is calculated from those same lines to two decimal
places. Leaving the due date blank means no due date. Changing currency does not
convert existing prices; selecting inventory products for USD invoices requires
a valid exchange rate, while manual USD price entry remains available.

Public transfer confirmations accept the same case-insensitive invoice IDs as
invoice viewing. Only pending invoices move to awaiting confirmation; cancelled
invoices are rejected. Repeated confirmations for paid or already-awaiting
invoices do not send duplicate notifications. Successful transitions invalidate
the invoice and seller-list caches.

### Expense report dates

Expense summaries and statistics share tax reporting's calendar validation,
including ISO week boundaries. Impossible dates and non-existent ISO weeks return
HTTP 400 with an explanation rather than a server error or a different period.
Years outside 1 through 9999 are rejected by request validation. Omitting period
parameters retains the existing current-period defaults.

### AI assistant review and recovery

AI advice remains review-first: product copy, featured selections, promotions,
purchase-order drafts, and collection messages are not applied or sent without
merchant approval. Background advice refreshes preserve unsaved selections and
reminder edits. Failures remain visible with retry controls; unavailable recovery
metrics are not presented as zero.

Collection reminders exclude invoices due today and drafts already dismissed
that day before applying the priority limit. Failed deliveries remain visible for
review: save the reviewed draft to re-enable sending, then explicitly confirm it.
Simply retrying a failed send without this review remains blocked. Reminder
validation trims whitespace and requires at least 10 message characters and at
most 180 subject characters; blank Copilot questions return validation errors.

Shopping-assistant budget and service prompts work without an AI provider.
Changing stores or cancelling a search aborts the request and ignores late
responses. Match cards use the displayed catalog's names and prices and disable
adding unavailable, unpriced, or already-added products.

### Ask SuoOps: web navigation and reviewed drafts

The authenticated web dashboard includes **Ask SuoOps**, opened with its floating
button or **Cmd/Ctrl+K**. Existing menus remain available. It provides page-specific
help and permission-aware links, including settings tabs and invoice filters.
Examples: "Open bank details", "Show unpaid invoices last month", and
"What does awaiting confirmation mean?"

Simple invoice commands such as "Create an invoice for Ada",
"Invoice Ada 50k for design", and "Invoice John $25.50 for design" prepare an
editable draft. Users must choose **Review invoice draft**, complete missing
details, and explicitly submit the existing invoice form. The first version
supports one explicitly priced line with quantity one; complex quantities,
multiple lines, discounts, or inferred dates are not guessed. Unsupported input
opens a blank draft with an explanation. Customer contact information is never
prefilled. An open invoice form cannot be replaced by an assistant draft.

`GET /ai/web-assistant/context` and rate-limited `POST /ai/web-assistant/ask`
require authentication and resolve workspace permissions server-side. They do
not create, send, charge, delete, or update business records. Common requests
work without a provider or AI allowance. Ambiguous requests can optionally use
the existing governed gateway under `web_navigation`; tenant opt-out, feature
controls, rollout, and monthly allowances apply. AI selects only server-owned
workflow IDs, never arbitrary URLs or business facts. Failures show a notice
and retain deterministic shortcuts. Users can disable optional interpretation
in the drawer or workspace AI controls.

Invoice links carry `status`, `start_date`, and `end_date`. "Unpaid" includes
pending and awaiting-confirmation invoices, not cancelled invoices. Date
filters use the existing due-date-first semantics, falling back to creation
date when no due date exists; this is explained in both the assistant and list.
Invalid or reversed dates return HTTP 422 instead of silently removing a filter.

### Storefront delivery quotes

Physical-order checkout waits for delivery quotes, including the debounce before
the request. A refreshed quote updates the selected courier's price; superseded
responses cannot overwrite the latest options. If quotes fail, buyers see an
error and can retry or explicitly choose self-pickup rather than silently ordering
without delivery. Service/digital-only orders do not request courier quotes.
Rapid repeated clicks submit only one order, and product QR links respect stores
that have disabled online payments.

### Dependency security checks

The dependency scan workflow fails on both known vulnerabilities and scanner
errors, and attempts to upload its JSON report even on failure. To reproduce the
scan locally, run `pip-audit -r requirements.txt --format json`. The workflow's
explicit advisory exception remains documented in its configuration.

Both frontend applications use ESLint's flat configuration and the ESLint CLI.
Their Next.js lint plugin is intentionally pinned to 14.2.35 with patched
`glob` 10.5: newer plugin versions depend on the vulnerable `braces` toolchain.
The 21 Next.js rules and recommended/core-web-vitals severities were verified
against the previous plugin. `@eslint/compat` adapts these rules to ESLint 9;
the Next.js application framework and its config package remain on 15.5.27.
Revisit the plugin pin once upstream removes or patches the affected dependency.

The support application now uses Tailwind CSS 4 and `@tailwindcss/postcss`.
Its theme is defined in `app/globals.css`; the former Tailwind configuration
was migrated by the official upgrade tool. Class-name changes preserve the
previous shadow, outline, and gradient behavior. Tailwind 4 requires modern
browsers (Safari 16.4+, Chrome 111+, Firefox 128+), matching the main application's
existing Tailwind 4 requirement.

## Observability & Metrics

Prometheus counters & histograms are exposed via the standard `/metrics` endpoint (enabled when the `prometheus_client` library is installed). Instrumentation lives in `app/metrics.py` behind semantic helper functions.

| Metric | Description |
| ------ | ----------- |
| `invoice_created_total` | Invoices successfully created |
| `invoice_paid_total` | Invoices marked paid |
| `payment_confirmation_latency_seconds` | Histogram: time from creation to payment confirmation |
| `whatsapp_parse_unknown_total` | WhatsApp messages with unknown intent |
| `oauth_logins_total` | Successful OAuth login callbacks |
| `tax_profile_updates_total` | Tax profile update operations |
| `vat_calculations_total` | VAT summaries or calculator hits |
| `compliance_checks_total` | Tax compliance summary requests |

### Usage Pattern
Add new metrics ONLY by defining them in `app/metrics.py` and providing a helper. Call helpers from routes/services:

```python
from app.metrics import tax_profile_updated

def update_tax_profile(...):
  # business logic
  tax_profile_updated()
```

If Prometheus isn't installed, helpers become debug logs (no exceptions).

### Suggested Alerts
Examples (pseudo rules):
* Spike in VAT calculations: `increase(vat_calculations_total[5m]) > 1000`
* Low invoice creation during business hours: `sum(invoice_created_total offset 1h) < EXPECTED_MIN`

See `deploy/prometheus.yml` for base Prometheus config.

## Next Steps

## Developer Tooling: Pre-Push Git Hook

Automated local guard that runs backend `pytest` and frontend `vitest` before allowing a push.

1. Enable shared hooks path (one time):
```bash
git config core.hooksPath .githooks
chmod +x .githooks/pre-push
```
2. Attempt a push; if tests fail the push is blocked with a clear message.
3. To override (emergencies only):
```bash
SKIP_PRE_PUSH=1 git push origin main
```

Hook location: `.githooks/pre-push` (bash) – safe, idempotent, installs missing deps on first run.

Slack integration was removed (can be re-added later with a webhook secret).


## License
Proprietary (add appropriate license text).
