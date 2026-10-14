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

### Storefront escrow safety

Apply migration `20261014_escrow_safety` before starting the updated API or worker.
Reviewed release approval is stored separately from buyer confirmation and still
waits for settlement. Payout retries retain their original reference and verify
it on the sending provider; only a confirmed failure permits a new transfer.
Pre-transfer validation failures are recorded as not submitted and may safely
retry after bank details or funding are fixed. Transport uncertainty is not
treated as a rejection.
The worker rechecks delivery/protection under the payout lock, including returned
or cancelled shipments. An in-flight payout cannot also be refunded.

Refund submission is reserved before contacting the collecting provider.
`refund_pending` means the outcome needs provider reconciliation, not that the
buyer has received money. It blocks another refund, seller payout and account
deletion and appears in the admin review queue. Do not clear this state or retry
until the provider's actual outcome has been verified. Historical `refunded`
means the provider accepted the refund request; bank arrival is not guaranteed
by that status.

Expired unpaid checkouts cannot start new payments. A late verified payment is
held for review rather than lost behind an expired checkout. Buyer code-based
order support remains available when a store is offline, while new purchases
remain disabled. Seller evidence can be submitted during a dispute without
releasing funds. Release codes are not reused across a seller's order history.

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

### Ongoing merchant retention outreach

The merchant lifecycle reuses Business Intelligence's health-score calculation,
Commerce Copilot, Collections, inventory advice, and Ask SuoOps. It is not a
second invoice/reminder sender. The twice-weekly `retention.refresh_merchants` task
maintains New, Activated, Engaged, Healthy, At Risk, Dormant, and Reactivated
states from non-cancelled invoices, recorded payments, expenses and explicit
stock work (including purchases, excluding automatic reversals). Logins and
unconverted storefront carts do not count as meaningful activity.

Rules prepare first-invoice help after 24 hours, first-payment help after three
days, collections review, inactivity nudges at 14/21/30 days, and declining-sales
recommendations. Decline requires at least six invoices in the preceding
28-day baseline and a greater-than-50% drop in the latest 14-day run rate.
Healthy merchants receive no retention outreach. Returning merchants stop
win-back and get seven days of reactivated nurture. Amounts remain separate by
currency; no invented amounts, bank-settlement claims, or automatic customer sends.
Optional AI narrative enhancement remains inside the existing governed Copilot.
Outbound template facts are deterministic, not model-written financial claims.

The first-invoice and record-keeping nudges encourage recording each sale,
confirming payment only when received, and keeping expense records for tax
review. They do not infer unrecorded sales or promise tax compliance/NRS-validated
invoices. The NTA commenced on 1 January 2026; this is distinct from phased
e-invoicing implementation (see the [government transition guidance](https://fmino.gov.ng/federal-government-issues-transition-guidelines-for-tax-acts-2025/)
and [NRS rollout summary](https://kpmg.com/us/en/taxnewsflash/news/2026/02/nigeria-phased-rollout-e-invoicing-electronic-fiscal-system.html)).
No blanket January 2027 deadline is included in outbound copy.

Collections and low-stock actions take priority. Otherwise, inactive invoicing
merchants with active physical products can discover the storefront even if
they have never set up a store, activated online payments or used a courier.
This introduction guides them to complete missing setup and describes courier
delivery conditionally. The order-ready "share your storefront" variant requires
an active store, configured online payments and courier checkout, contact/location
details, and a complete, purchasable physical listing. Neither variant requires
previous courier orders or awareness of the feature. The message
explains buyer-paid delivery, the seller's package photo and booking request,
waiting for confirmation, and the difference between pickup and drop-off.
Options remain subject to address and courier availability; no live quote is
requested merely to prepare a nudge. Personalized messages use existing Brevo
SMTP; no new Meta template is needed.

Rollout (nothing sends merely by deploying):

1. Apply Alembic through `20261014_ongoing_retention` before starting updated
   web/worker processes. Historical experiment/contact data remains intact.
2. Reuse `BREVO_SMTP_LOGIN` and `BREVO_SMTP_KEY` for email. ZeptoMail remains
   available for transactional messages, never as a retention-email fallback.
   Email is preferred for existing merchants with an address who have not opted
   out or been suppressed for bounces/complaints.
3. WhatsApp requires explicit, verified-number-bound consent in **Settings >
   AI controls > Business nudges** or `START NUDGES`. Phone verification alone
   is not consent. The existing `WHATSAPP_TEMPLATE_UNPAID_ALERT` is used only
   for outstanding NGN invoices, with its established name/amount/count fields.
   Arbitrary tax/storefront text is not inserted into a fixed Meta template.
4. Set `RETENTION_OUTREACH_ENABLED=true` on both API and worker, then enable
   **Business Intelligence > Merchant retention outreach**, or use authorized
   `PATCH /admin/retention/outreach` with `{"enabled": true}`. Both gates are
   required. Pause through the same control with `false`.
5. The existing task runs Mondays and Thursdays at 09:00 UTC (10:00 WAT).
   The one-message-per-merchant-per-week limit remains unchanged. A super admin
   can start it immediately via `POST /admin/tasks/retention/trigger`. Processing batches
   of 50 are pagination, not a pilot or recipient cap. There is no enrollment,
   control group or 30-day expiry. Internal/test, team-member and flagged
   accounts remain excluded; only merchants with useful recommendations qualify.

Email includes a signed unsubscribe link and one-click unsubscribe headers.
GET shows confirmation without changing preferences; POST opts out. Merchants
can also change email and WhatsApp preferences in settings. WhatsApp `STOP`,
`STOP NUDGES` and `UNSUBSCRIBE` stop business nudges without switching them to
email. `SUMMARY` still opens their existing grounded Copilot briefing.
Unsubscribing does not disable account login, invoice or receipt emails.

At most one nudge per merchant per seven days across both channels; WhatsApp
also respects the existing shared budget and recent legacy lifecycle emails.
Database reservations and unique episode keys prevent overlapping workers from
sending the same action again. Reservations persist before the provider call;
ambiguous failures are surfaced as unknown and **not retried automatically**.
SMTP acceptance or a WhatsApp provider message ID counts as accepted, not inbox
delivery. WhatsApp delivery webhooks establish delivery separately.
Re-check provider logs before manually resolving an unknown
outcome. Never reset the dedupe key merely to force a retry.

When the global flag is enabled, this engine replaces overlapping activation,
win-back, aggregate unpaid, growth, expense-habit, feedback and morning-tip
campaigns, including while the admin control is paused. Explicit opt-outs and
recent retention contacts also suppress those merchant campaigns if the flag
is later disabled. Normal transaction notifications and existing customer
collections behavior are unchanged. Historical experiment exclusions remain
respected; obsolete pilot-creation controls have been removed.

The panel reports activation, matured 30/60/90-day retention, mean time to first
invoice/payment, GMV and recorded commission per active merchant, conversion,
and accepted outreach by channel. Retention means meaningful activity in days
N through N+6 after signup, excluding immature cohorts. GMV is based on recorded
paid invoices, including merchant-marked payments, not verified bank settlement.
Only persisted commission fees are counted; missing historical fees are explicit.
Naira GMV is not mixed with other currencies.

Ongoing activity-after-contact is measured after accepted submission and is
observational, not proof that outreach caused the return. Zero denominators
return null rather than a misleading 0%. Historical experiment reporting is
preserved separately with its original fixed arms, delivery-based attribution
and fixed cutoff; ongoing contacts are not added to those experiment results.

### Storefront delivery quotes

Physical-order checkout waits for delivery quotes, including the debounce before
the request. A refreshed quote updates the selected courier's price; superseded
responses cannot overwrite the latest options. If quotes fail, buyers see an
error and can retry or explicitly choose self-pickup rather than silently ordering
without delivery. Service/digital-only orders do not request courier quotes.
Rapid repeated clicks submit only one order, and product QR links respect stores
that have disabled online payments.

### Courier booking confirmation

Selecting a courier at checkout records the buyer's choice and delivery fee;
it does not summon a rider. For paid pickup orders the seller packs the goods,
opens **Book courier pickup**, attaches the package photo and submits
**Request courier booking**. Drop-off orders use **Book drop-off delivery**;
after confirmation, the seller takes the parcel to the designated station.

Booking requests persist as `booking_pending`, are claimed as
`booking_in_progress` before the provider call, and become `booked` only with a
provider order ID. The seller panel briefly polls pending requests. Booking is
not dispatch: courier movement webhooks establish that the parcel is on its
way. Late earlier-progress webhooks cannot move delivery status backwards.
Manual dispatch and service/digital orders keep their existing flows.

An explicit provider rejection becomes `booking_failed` and permits a
merchant-initiated retry using the saved package photo. Timeouts, malformed
responses and ambiguous server failures become `booking_unknown`; neither
repeated clicks nor repeated background callbacks can book again. Interrupted
pending/in-progress requests and legacy sent-out courier orders without a
provider ID require operator reconciliation, not a blind retry. Check the
Shipbubble booking records before resolving them; if a booking exists, restore
its ID/tracking instead of creating another one.

Selected courier orders cannot auto-pay out merely because their original
payment window elapsed, including when booking is pending, failed or unknown.
They require courier delivery plus the inspection window, or buyer confirmation.
Overdue undelivered/unconfirmed bookings enter the existing admin-review flow.
No schema migration or delivery/outreach flag enablement is part of this change.

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
