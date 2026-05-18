# 📬 Project-Weekly-Outreach — Execution Flow

> A complete walkthrough of how the system starts, schedules, and sends outreach emails — from the first database flag all the way to the inbox.

---

## 🗺️ Overview

```
Admin flips a flag in DB
        ↓
MySQL Trigger creates a Schedule
        ↓
Scheduler (run_scheduler.py) detects it
        ↓
Snapshot fills the email queue from outreach_emails
        ↓
Dispatch claims a batch
        ↓
Celery Worker sends the emails
        ↓
Results reported → Daily HTML Report sent to admin
```

---

## Phase 1 — Activation (Database Trigger)

**Trigger:** Admin sets `run_outreach_emails = 1` for a candidate in the `candidate_marketing` table.

### What happens automatically:

A MySQL trigger (`trg_candidate_marketing_outreach`) fires **instantly** on that update.

It does the following:
1. **Deduplication check** — If an active schedule already exists for this candidate, it skips silently.
2. **Fetches candidate details** — Reads `full_name`, `linkedin_id`, and `email` from the `candidate` table.
3. **Validates the data** — Fails loudly if `full_name` or `email` is missing.
4. **Calculates next run time** — Always targets the next weekday (Mon–Fri) at **9:00 AM** with a random ±30 minute offset to avoid spam detection patterns.
5. **Creates a schedule row** in `automation_workflows_schedule` for `workflow_id = 3` (weekly vendor outreach).

```
candidate_marketing (run_outreach_emails: 0 → 1)
        ↓  [TRIGGER fires]
candidate table  →  reads full_name, linkedin_url, email
        ↓
automation_workflows_schedule  →  INSERT new schedule row
                                  next_run_at = next weekday 9AM ± random offset
                                  enabled = 1
```

> **No Python code runs yet. This is pure database-level automation.**

---

## Phase 2 — Scheduler Detects the Schedule

**File:** `run_scheduler.py` → calls `campaign_scheduler.py`

The scheduler runs **continuously** (every 60 seconds by default) or can be triggered manually:

```bash
# Run once manually
python run_scheduler.py --once

# Run continuously (production)
python run_scheduler.py
```

### What the scheduler does each cycle:

1. **Calls** `GET /orchestrator/schedules/due` on the FastAPI backend.
2. Backend returns all schedules where `next_run_at <= NOW()` and `enabled = 1`.
3. For each due schedule, the scheduler:
   - Reads `candidate_id` and `run_parameters` (name, linkedin URL, etc.) from the schedule row.
   - **Locks the schedule** via `POST /orchestrator/schedules/{id}/lock` to prevent duplicate runs.
   - Creates a **run log** entry via `POST /orchestrator/logs`.
4. **Fetches the execution bundle** from `GET /automation-workflow/{workflow_id}/execution-bundle`:
   - Gets the email **template** (subject + body with Jinja2 variables).
   - Gets all active **SMTP credentials** for this candidate.
5. **Calculates today's remaining send capacity** — respects per-account daily limits and warmup limits.
6. If limit is already reached → logs and skips this cycle.

```
run_scheduler.py
        ↓
GET /orchestrator/schedules/due
        ↓
[For each due schedule]
        ↓
Lock schedule → Create run log → Fetch bundle (template + SMTP creds)
        ↓
Calculate daily limit remaining per SMTP account
```

---

## Phase 3 — Snapshot (Build the Email Queue)

**File:** `campaign_email_utils.py` → `generate_snapshot()`
**API call:** `POST /campaign-emails/candidates/{candidate_id}/snapshot`

The snapshot is the step that **fills the email queue** from your contact list.

### Source table: `outreach_emails`

The system queries `outreach_emails` and copies eligible contacts into `campaign_emails`:

```sql
INSERT IGNORE INTO campaign_emails (workflow_id, candidate_id, vendor_email, ...)
SELECT ...
FROM outreach_emails oe
WHERE oe.status           = 'ACTIVE'
  AND oe.validation_status = 'VALID'
  AND oe not already emailed for this candidate/scheduler
```

**Filters applied (contacts excluded):**
| Status | Excluded? |
|---|---|
| `ACTIVE` + `VALID` | ✅ Included |
| `BOUNCED` | ❌ Excluded |
| `UNSUBSCRIBED` | ❌ Excluded |
| `SUPPRESSED` | ❌ Excluded |
| `INVALID` | ❌ Excluded |
| `COMPLAINED` | ❌ Excluded |
| Already emailed (this candidate) | ❌ Excluded |

> **This operation is idempotent** — safe to run multiple times. `INSERT IGNORE` prevents duplicates.

---

## Phase 4 — Dispatch (Claim a Batch)

**File:** `campaign_email_utils.py` → `dispatch_pending()`
**API call:** `POST /campaign-emails/candidates/{candidate_id}/dispatch?limit=N`

The scheduler claims up to `N` emails (based on SMTP daily limit remaining):

```sql
SELECT id, vendor_email
FROM campaign_emails
WHERE candidate_id = :candidate_id
  AND status = 'pending'
LIMIT :limit
FOR UPDATE SKIP LOCKED   ← prevents concurrent workers grabbing the same row
```

Claimed rows are immediately updated: `status = 'processing'`

The list of `{id, vendor_email}` pairs is returned to the scheduler.

---

## Phase 5 — Celery Workers Send the Emails

**File:** `app/workers/email_worker.py`

The scheduler enqueues one Celery task per email, with a **randomized delay of 30–120 seconds** between each to simulate human sending behavior.

Each Celery task does:
1. **Sanitizes** the target email address (removes leading hyphens, bullet points, junk characters).
2. **Renders the personalized template** — injects `candidate_name`, `linkedin_url`, etc. via Jinja2.
3. **Rotates SMTP credentials** — picks one active SMTP account from the pool.
4. **Sends the email** via SMTP or Gmail API.
5. **Reports the result** back to the API:
   - ✅ **Success** → `campaign_emails.status = 'sent'`
   - ❌ **Hard bounce** → `status = 'bounced'`, `bounce_type = 'hard'` → suppressed forever
   - ⚠️ **Soft bounce** → `status = 'bounced'`, `bounce_type = 'soft'` → retryable
   - 🚫 **Invalid email** → `status = 'bounced'`, `bounce_type = 'invalid'` → suppressed forever

```
Celery Task received
        ↓
Sanitize email → Render template → Pick SMTP account
        ↓
Send email
        ↓
Success?  →  Update status = 'sent'
Failed?   →  Classify bounce type → Update status + bounce_type
```

---

## Phase 6 — Daily Report (6 PM Auto-trigger)

**File:** `run_daily_report.py`

At **6:00 PM** each day, the scheduler automatically triggers the daily report (via Redis to ensure it runs exactly once per day).

The report aggregates all activity for that day and sends a premium HTML email to the admin showing:
- Total dispatched / sent / failed / bounced
- Per-candidate breakdown with success rates
- Campaign progress bar
- Bounce breakdown (hard / soft / invalid)

---

## 🔄 Full Lifecycle Summary

```
1. Admin sets run_outreach_emails = 1
        ↓
2. MySQL Trigger → creates schedule in automation_workflows_schedule
        ↓
3. run_scheduler.py (running every 60s) → detects due schedule
        ↓
4. Locks schedule → fetches template + SMTP creds from backend
        ↓
5. POST /snapshot → reads outreach_emails → fills campaign_emails as 'pending'
        ↓
6. POST /dispatch → claims batch (FOR UPDATE SKIP LOCKED) → marks 'processing'
        ↓
7. Celery tasks enqueued (30–120s staggered delays)
        ↓
8. Workers send emails → update status (sent / bounced / failed)
        ↓
9. At 6 PM → Daily HTML report emailed to admin
        ↓
10. When all emails exhausted → schedule disabled → run_outreach_emails reset to 0
```

---

## 📂 Key Files Reference

| File | Role |
|---|---|
| `run_scheduler.py` | Entry point — runs the scheduler loop |
| `app/scheduler/campaign_scheduler.py` | Core scheduling logic — orchestrates all phases |
| `app/workers/email_worker.py` | Celery worker — sends emails, classifies bounces |
| `app/services/report_service.py` | Builds and sends the daily HTML report |
| `app/services/email_service.py` | Low-level SMTP/Gmail send logic |
| `run_daily_report.py` | Standalone daily report trigger |
| `run_analytics.py` | DuckDB analytics export |

## 🗄️ Key Tables Reference

| Table | Role |
|---|---|
| `candidate_marketing` | Flag (`run_outreach_emails`) that starts everything |
| `candidate` | Source of candidate name, email, LinkedIn URL |
| `automation_workflows_schedule` | The schedule row created by the DB trigger |
| `outreach_emails` | **Source of recruiter/vendor emails to contact** |
| `campaign_emails` | Working queue — tracks every email send attempt |
| `automation_workflow_logs` | Audit log of each scheduler run |

---

