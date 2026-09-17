# Compliance notes (United Kingdom first)

> Engineering notes to prepare for formal assessment. Not legal advice; validate with the
> organisation's DPO, Clinical Safety Officer and counsel.

## Product classification

CareOS routes and records alarms and coordinates human responders. It **does not** diagnose,
predict, monitor physiological parameters or recommend treatment. Keeping this boundary is
what keeps it outside UK MDR 2002 software-as-a-medical-device scope; any feature that
interprets health data (e.g. fall-risk prediction) requires a regulatory assessment first.

## Frameworks likely to apply

| Framework | Relevance | Current preparation |
|---|---|---|
| UK GDPR / Data Protection Act 2018 | special-category-adjacent personal data of vulnerable adults | data minimisation, role-based access, audit, isolation, redacted logs |
| DCB0129 (manufacturer) / DCB0160 (deploying organisation) | clinical risk management for health IT used in care pathways | deterministic core, fail-safe escalation, hazard-relevant tests; hazard log to be created |
| NHS DSPT | required by many care providers and NHS-connected services | security controls in `docs/security` |
| TEC Services Association (TSA) Quality Standards Framework | UK telecare/ARC service standards (call handling, escalation) | configurable escalation policies, timestamps, audit |
| BS 8521-1/-2, EN 50134 (SCAIP) | alarm protocol interoperability | adapter architecture; SCAIP receiver planned |
| CQC (service providers) | evidence of safe care processes | incident timeline, resolution records |
| Cyber Essentials Plus | common supplier requirement | container hardening, patching policy |

## Data inventory (MVP)

| Data | Purpose | Minimisation |
|---|---|---|
| Service user name, phone, city, optional address/postcode, home coordinates | reach and locate the person | no date of birth, NHS number, diagnoses, medications or care notes |
| Trusted contacts: name, relationship, phone, optional email, priority | escalation | only what calling requires |
| Device telemetry: battery, signal, last location | device health, responder location | snapshot only, no location history table |
| Incident timeline and resolution notes | safety record, review | notes are free text: staff guidance needed to avoid unnecessary health detail |
| Staff accounts, sessions (IP, user agent), audit logs | security and accountability | token digests only, no passwords |

Demo seed data is fictional; phone numbers use Ofcom ranges reserved for drama.

## Retention (proposal, to confirm with customers)

* Incidents, timeline, audit: 8 years (aligned with typical adult social care records guidance).
* Sessions: 90 days after expiry. Device receipts: 2 years.
* Deletion requests: service-user records are soft-deleted; incident history is retained under
  legal obligation/vital interests with restricted access.

## Hosting and transfers

Primary region UK (e.g. AWS eu-west-2 London). No personal data leaves the UK/EEA; AI providers
must meet this or run without personal data. Sub-processor list to be maintained per tenant.

## Open actions before first customer

1. DPIA template and Data Processing Agreement.
2. Clinical safety case and hazard log (DCB0129) with a named Clinical Safety Officer.
3. Records retention jobs and subject access export.
4. Business continuity: RPO/RTO targets, backup restore drills, alarm-flow failover test.
