# AegisOps

**A governed autonomous incident-response prototype**: an LLM agent investigates
real injected failures in a Docker microservice lab — with evidence-verified
claims, an independent root-cause gate, and human-approved remediation, instead
of letting the model freewheel.

> **Core principle: no LLM-generated arbitrary command may ever touch a managed
> production resource.** Investigation is read-only; remediation actions come
> from a human-reviewed mapping table; execution passes OPA policy, independent
> approval, and precondition re-checks.

[[Blind-eval results](#blind-evaluation-results)] · [[Quick start](#quick-start)] ·
[[Architecture](#architecture)] · [[Honest boundaries](#honest-capability-boundaries)] ·
[[中文说明](README.md)]

## What it is

An incident-response system running on a local Docker lab (gateway / order /
inventory / payment services + Redis + PostgreSQL + a full telemetry stack):

1. **Real fault injection** (container pause/stop, cache outage, connection-pool
   exhaustion, bad deployment, …) produces real alerts, metrics, logs, traces,
   and request-failure events.
2. **Autonomous investigation**: within a bounded budget (4 model calls /
   8 tools / 90 s) the model chooses read-only tools, updates candidate
   hypotheses, and files **structured, verifiable claims**.
3. **Independent verification**: a backend verifier checks every claim's
   identity, timing, and provenance against completed tool records — the
   model's word is never enough. A root-cause gate additionally requires
   *verified mechanism × full direct-source coverage*.
4. **Governed remediation**: a gate-passed candidate reaches a deterministic
   mapping table → plan → OPA policy → independent human approval → fenced
   executor (precondition re-checks) → recovery assessment → resolution.
5. **Full audit trail**: every decision is persisted; incident timelines are
   replayable.

```text
real fault → alerts/signals → autonomous investigation (read-only, budgeted)
                          → observation verification → root-cause gate
                                                            ↓ passed
 resolution ← recovery check ← fenced execution ← approval ← OPA ← mapping
```

## Blind evaluation results

Frozen manifest (`eval/agent-blind-manifest-v2.json`), ground truth strictly
separated from model input, post-hoc scoring (methodology anchored on
[RCAEval](https://github.com/phamquiluan/RCAEval) and
[AIOpsLab](https://github.com/microsoft/AIOpsLab)), unknown never counts as
correct:

| Metric | Result (current config, 6 fault cases + 1 control × 1 run) |
|---|---|
| Localisation correct (verified claim points at the true root service) | **4/6 = 0.667** (chance 0.2, lift ≈ 3.3) |
| Mechanism exact (additionally matches the injected mechanism identity) | 2/6 |
| Wrong claims | **0** |
| Scope-limited cases (truth outside the read-only tool scope) | 2/6 — honest exits, zero wrong claims |
| Quiet control | zero false positives |
| Cost | ~11k tokens per case |

Full denominators and per-run records: `eval/agent-blind-summary-*.json`.
Small sample, one run per case — **this is not a claim of general diagnostic
capability**. Method and boundaries:
[docs/agent-blind-eval-2026-10-24.md](docs/agent-blind-eval-2026-10-24.md).

The end-to-end governed chain (a real Redis fault through the whole loop) is
verified: [docs/agent-remediation-loop-2026-10-25.md](docs/agent-remediation-loop-2026-10-25.md).

## Quick start

Requires Python 3.12+ and Docker Desktop. Local port conventions: API
`18000`, console `18010`, business DB host port `5434` (see
`compose.override.yaml`; adjust freely to avoid collisions on your machine).

```bash
git clone https://github.com/hzz-0005/aegisops && cd aegisops
python -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
docker compose up -d          # 24 containers: business lab + control plane + telemetry
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 18000
# second terminal:
.venv/bin/python -m uvicorn apps.lab_console.main:app --host 127.0.0.1 --port 18010
```

- Storefront & incident workbench: <http://127.0.0.1:18010/> ·
  <http://127.0.0.1:18010/?view=ops>
- Works without an LLM key: the agent stops gracefully and the deterministic
  rule baseline still investigates.
- Configure any Chat-Completions-compatible endpoint by copying
  `.env.example` to `.env`.
- One-command demo: `./demo.sh` (inject → promote → investigate → gate →
  approve → execute → recover → resolve).

## Architecture

```text
apps/            API (FastAPI) · local console · Temporal worker · isolated lab-executor
src/aegisops/    domain models · correlation engine · autonomous loop
                 (agent_loop / verifier / root-cause gate) · remediation
                 governance (OPA client / approval / fenced execution /
                 recovery assessment) · request-signal detector
lab/             business microservices (gateway/order/inventory/payment)
                 + fault-injection scenarios
infra/opa/       remediation policy (Rego, with tests)
migrations/      27 Alembic migrations (no create_all at startup)
```

**Key governance design**:

- The agent loop runs **inside the investigation Activity** (LLMs never enter
  deterministic workflow code); a database journal reserves before every paid
  call — interruptions do not reset budgets or repeat calls (verified with a
  real worker kill).
- **The verifier only trusts backend records**: claims must match completed
  tool facts by identity/time/provenance; model-declared conflicts are
  advisory only; forged citations fail closed.
- **Malformed model output retries inside the same budget** — validation is
  never relaxed, so failure stays safe.
- The **executor is isolated** from the control plane (the only container with
  the Docker socket); it re-checks OPA, approval, snapshot hashes, and real
  fault preconditions before acting; uncertain outcomes go to manual
  reconciliation instead of blind retries.
- **Non-dev profiles refuse to start** (real identity/tenancy is a
  production prerequisite — see
  [docs/production-adapter-contract.md](docs/production-adapter-contract.md)).

## Verification

```bash
.venv/bin/python -m pytest     # 356 tests
.venv/bin/python -m ruff check .
docker compose run --rm --no-deps opa test /policy
```

Resilience verified: real Temporal Activity interruption (honest failure +
expired-journal takeover), uncertain-outcome isolation, concurrent reservation
fencing, cross-tenant isolation.

## Honest capability boundaries

- Verified execution adapters are the three local-lab kinds (Redis restart /
  order restart / order rollback); the action mapping currently has exactly
  one entry (Redis cache) — **expansion is reviewed case by case; the model
  never produces action parameters**.
- Blind-eval sample is small (see above); alert grouping and request-propagation
  partitioning remain research areas — no automatic incident merging.
- Fixed dev tenant / static tokens; no production identity, tenancy isolation,
  or real payment/deployment integration.
- This is a **local-lab prototype**, not a production system; production
  adaptation boundaries are in the contract doc above.

## License

Apache-2.0 — see [LICENSE](LICENSE).
