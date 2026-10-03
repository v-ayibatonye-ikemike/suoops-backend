# AI Governance and Controlled Rollout

SuoOps AI features assist commerce workflows but do not own financial, inventory,
customer-contact, storefront-change, or dispute-resolution decisions.

## Control hierarchy

Controls are evaluated in this order:

1. `AI_ENABLED` is the deployment-level emergency master switch.
2. The database feature kill switch must be enabled.
3. The merchant workspace must have optional AI assistance enabled.
4. A merchant-level feature override must not disable the feature.
5. The workspace must be in the deterministic rollout percentage or explicit
   canary allowlist.
6. The normal monthly quota must have capacity, except internal admin dispute
   reviews, which are separately attributed and do not consume merchant quota.

No lower-level control can override a higher-level denial.

## Allowlisted generative features

- Commerce Copilot narratives.
- Collections reminder tone.
- Inventory Adviser explanations.
- Storefront product copy.
- Buyer shopping ranking.
- Dispute evidence summaries.

Adding another model call requires adding it to the central feature registry,
documenting its source facts and prohibited actions, and adding fallback tests.
Unknown feature keys fail closed.

## Rollout procedure

1. Keep the feature disabled while its deterministic fallback and validation
   tests are completed.
2. Enable a named canary allowlist with rollout at 0%.
3. Review failures, blocked operations, latency, cost, and user feedback.
4. Increase deterministic rollout in small steps (for example 5%, 25%, 50%,
   then 100%). Assignment is stable per feature and workspace.
5. Record the reason for every control change.
6. Stop or reverse rollout when error rate, cost, harmful output, or privacy
   risk exceeds the approved threshold.

## Incident response

- Disable the affected database feature control first.
- Use `AI_ENABLED=false` when the incident affects multiple features or the
  provider boundary.
- Deterministic calculations and fallbacks remain available.
- Review the AI usage ledger by feature, prompt version, provider, model,
  status, error code, cost, and admin/user actor.
- Do not copy prompt content into tickets or logs. The ledger stores hashes and
  operational metadata, not prompt bodies.

## Human control

- Collection messages, product copy, promotions, bundles, and purchasing drafts
  require explicit merchant approval.
- Buyer recommendations can only identify server-verified catalog products.
- Dispute AI cannot call refund, release, suspension, or card-block tools.
- Financial amounts, margins, inventory quantities, stock, prices, and escrow
  state are calculated by domain services, not language models.

## Retention and review

The weekly retention task deletes AI usage metadata and feedback after 18 months,
cached Copilot narratives after 30 days, and old action/draft records according
to the published retention policy. Governance controls and tenant preferences
remain while the related account or platform control exists.
