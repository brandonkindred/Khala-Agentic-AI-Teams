"""
SLA best-practice catalog for planning agents.

Cost-sensitive defaults: favor pragmatic, simpler topologies over ultra-premium
redundancy unless the spec explicitly demands extreme HA or low latency.
"""

SLA_BEST_PRACTICES_CATALOG = """
**SLA Best-Practice Defaults (cost-sensitive):**

1. **Availability / uptime:**
   - Internal line-of-business or backoffice tools: 99.5%–99.8% (single-region HA, managed DB with multi-AZ, health checks).
   - External customer-facing core flows (auth, payments, main app): 99.9% with clear error budgets; multi-AZ and robust auto-recovery; NOT multi-region active-active by default.

2. **Latency / performance:**
   - User-facing UI interactions: p95 page load or primary action under ~500ms on modern connections; background or reporting flows can tolerate higher.
   - Public APIs: p95 < 500–800ms, with explicit timeouts and backpressure; internal batch/analytics APIs may be higher.

3. **RTO/RPO and data durability:**
   - General transactional apps: RPO ≤ 15 min, RTO ≤ 1–2 hours; periodic automated backups, point-in-time restore, infra-as-code to re-provision.
   - For clearly critical financial or compliance systems: bump targets up one tier and call out cost impact.

4. **Incident response / support SLAs:**
   - Detection and alerting: on-call alert for severity-1 within 5 minutes, acknowledgment within 15 minutes, mitigation within 2–4 hours.
   - Support response (if spec hints at support team): first response within 1 business day for normal tickets, faster for critical.
"""

SLA_ENTERPRISE_ANCHORING_GUIDANCE = """
**Enterprise anchoring (when resolving SLA questions):**
- Use cloud provider and devtools patterns (e.g. typical AWS/Azure/GCP managed service SLAs, Datadog/PagerDuty-style incident practices) as mental models.
- Favor managed services and simpler topologies as defaults unless the spec explicitly demands extreme HA or low latency (cost-sensitive bias).
- Explicitly mention trade-offs (cost, complexity, vendor lock-in) in the justification.
"""
