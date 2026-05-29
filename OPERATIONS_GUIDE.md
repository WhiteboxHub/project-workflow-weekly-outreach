# Weekly Outreach — Operations Guide

How to run **Project-Weekly-Outreach** locally and in production without issues.

This document covers prerequisites, startup order, pre-flight checks, testing, and common failures (based on real troubleshooting).

---

## Table of contents

1. [What this system does](#what-this-system-does)
2. [Architecture (4 moving parts)](#architecture-4-moving-parts)
3. [Before you run anything](#before-you-run-anything)
4. [Local development](#local-development)
5. [Local test send (2 emails)](#local-test-send-2-emails)
6. [Activating a candidate (database)](#activating-a-candidate-database)
7. [Production deployment](#production-deployment)
8. [Pre-flight checklists](#pre-flight-checklists)
9. [DuckDB maintenance (local mode)](#duckdb-maintenance-local-mode)
10. [How to verify emails sent](#how-to-verify-emails-sent)
11. [Troubleshooting](#troubleshooting)
12. [Known limitations](#known-limitations)

---

## What this system does

1. **MySQL trigger / admin flag** — Sets `candidate_marketing.run_outreach_emails = 1` and creates a schedule.
2. **Scheduler** (`run_scheduler.py`) — Polls `GET /api/orchestrator/schedules/due`, builds a send batch, enqueues Celery tasks.
3. **Celery worker** — Sends email via SMTP using credentials from the FAPI backend.
4. **Optional local DuckDB** (`USE_LOCAL_DUCKDB_CAMPAIGNS=true`) — Stores campaign state locally; reduces API calls for multi-step sequences.

**Important:** The scheduler does **not** send email by itself. The **Celery worker** must be running.

---

## Architecture (4 moving parts)

```
MySQL (schedules, SMTP creds, vendors)
        ↓
FAPI Backend (wbl-backend) — port 8000
        ↓
Scheduler (run_scheduler.py) — enqueues jobs
        ↓
Redis — message broker
        ↓
Celery Worker — sends SMTP email
        ↓
(Optional) DuckDB — local campaign state
```

| Component | Required? | Default port |
|-----------|-----------|--------------|
| MySQL | Yes | 3306 |
| FAPI backend (`wbl-backend`) | Yes | 8000 |
| Redis | Yes | 6379 |
| Celery worker | Yes | — |
| Scheduler | Yes | — |
| Docker | Optional (Redis) | — |

---

## Before you run anything

### 1. Environment file (`.env`)

Copy `.env.example` to `.env` and set:

| Variable | Purpose |
|----------|---------|
| `MAIN_API_BASE_URL` | Must match backend, e.g. `http://127.0.0.1:8000/api` |
| `REDIS_URL` | e.g. `redis://localhost:6379/0` |
| `API_LOGIN_EMAIL` / `API_LOGIN_PASSWORD` | Auto-refresh JWT on 401 |
| `API_BEARER_TOKEN` | Optional fallback if Redis empty |
| `USE_LOCAL_DUCKDB_CAMPAIGNS` | `true` = local sequence engine |
| `DUCKDB_CAMPAIGN_PATH` | e.g. `./data/campaigns.duckdb` |
| `REPORT_*` | Daily HTML report SMTP (optional) |

### 2. Python environment

```powershell
cd Project-Weekly-Outreach
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

Verify:

```powershell
python -c "import celery; print('ok')"
celery --version
```

### 3. Database / candidate prerequisites

For each candidate you want to outreach:

| Check | Table / location |
|-------|------------------|
| Marketing active | `candidate_marketing.status = 'active'` |
| Outreach enabled | `candidate_marketing.run_outreach_emails = 1` |
| Name for templates | `candidate.full_name` |
| LinkedIn in templates | `candidate.linkedin_id` (used as `linkedin_url`) |
| Vendor list | `outreach_email_recipients` — `ACTIVE`, valid emails |
| SMTP accounts | `email_smtp_credentials` — `is_active = 1`, valid **Gmail App Password** |
| Workflow active | `automation_workflows` id **3** (weekly outreach) — `status = 'active'` |

### 4. Gmail SMTP (critical)

For each row in `email_smtp_credentials`:

- Use a **16-character App Password**, not your normal Gmail password.
- Enable **2FA** on the Google account → create App Password at https://myaccount.google.com/apppasswords
- Log into that Gmail account in a browser once if Google asks.
- Store password in `app_password` (or `password` column).

Error if wrong: `534 5.7.9 WebLoginRequired`.

### 5. Schedule `run_parameters` must include

```json
{
  "candidate_id": 799,
  "candidate_name": "Full Name",
  "linkedin_url": "https://www.linkedin.com/in/...",
  "candidate_email": "sender@example.com"
}
```

Missing `candidate_name` or `linkedin_url` → scheduler **aborts** that run (by design).

---

## Local development

### Startup order (do not skip)

Start in this order. **Celery worker before scheduler** when testing.

#### Terminal 1 — Redis

Start **Docker Desktop**, then:

```powershell
cd Project-Weekly-Outreach
docker-compose up -d redis
docker ps   # should show email_outreach_redis on 6379
```

#### Terminal 2 — FAPI backend

```powershell
cd ..\wbl-backend
# activate backend venv if you use one
uvicorn fapi.main:app --reload --host 127.0.0.1 --port 8000
```

Verify: http://127.0.0.1:8000/docs

#### Terminal 3 — Celery worker

**Windows must use `--pool=solo`:**

```powershell
cd Project-Weekly-Outreach
.\venv\Scripts\activate
celery -A app.workers.celery_app worker --loglevel=info --pool=solo
```

Wait for: `celery@... ready`

#### Terminal 4 — Scheduler

**Continuous (normal):**

```powershell
python run_scheduler.py
```

**One-shot (testing):**

```powershell
python run_scheduler.py --once
```

### Windows notes

| Topic | Note |
|-------|------|
| Celery on Windows | Always `--pool=solo` |
| PowerShell `python -c "..."` | Nested quotes break; use helper scripts below |
| OneDrive paths | Use quotes around paths with spaces |

---

## Local test send (2 emails)

Use this to confirm SMTP works **without** waiting until Monday or sending 1000 emails.

### Step 1 — Force schedule due (MySQL)

```sql
UPDATE automation_workflows_schedule
SET next_run_at = UTC_TIMESTAMP(),
    enabled = 1
WHERE id = 19;   -- your schedule id
```

### Step 2 — Force 2 recipients due (DuckDB / weekend bypass)

Open `./data/campaigns.duckdb` (DuckDB CLI, DBeaver, or Python) and run:

```sql
-- Use your latest campaign_id (query campaigns table if unsure)
UPDATE campaign_recipients
SET next_send_at = CURRENT_TIMESTAMP
WHERE campaign_id = 3
  AND status = 'active'
  AND id IN (
    SELECT id FROM campaign_recipients
    WHERE campaign_id = 3 AND status = 'active'
    ORDER BY id
    LIMIT 2
  );
```

Verify:

```sql
SELECT COUNT(*) FROM campaign_recipients
WHERE campaign_id = 3 AND status = 'active' AND next_send_at <= CURRENT_TIMESTAMP;
-- Expected: 2
```

### Step 3 — Run scheduler (worker must already be `ready`)

```powershell
python run_scheduler.py --once
```

Expected logs:

- `local_campaign_tasks_enqueued` with `count=2`
- `emails_enqueued=2`

### Step 4 — Wait and verify

Wait **90–120 seconds** (random per-email delay), then check DuckDB:

```sql
SELECT id, vendor_email, status, sent_at
FROM campaign_email_attempts
WHERE status = 'sent'
ORDER BY sent_at DESC
LIMIT 10;
```

Expected: rows with `status = 'sent'`.

Worker should show: `local_campaign_email_sent_successfully`

### If a previous test left stuck rows

```sql
UPDATE campaign_email_attempts
SET status = 'pending', claimed_at = NULL, claimed_by = NULL
WHERE status = 'claimed';
```

Then repeat from Step 2.

---

## Activating a candidate (database)

### Normal activation

```sql
UPDATE candidate_marketing
SET run_outreach_emails = 1
WHERE candidate_id = YOUR_CANDIDATE_ID
  AND status = 'active';
```

If trigger `trg_candidate_marketing_outreach` exists, a schedule row is created with `next_run_at` on the **next weekday ~9 AM** (America/Los_Angeles).

### Business day behavior (local DuckDB mode)

| Situation | When step 1 sends |
|-----------|-------------------|
| Weekday, 9 AM–5 PM | Soon (with 30–120s jitter) |
| Weekday, outside window | Next business day 9 AM |
| **Saturday / Sunday** | **Soon** (step 1 sends immediately to capture interest; follow-ups defer to Monday) |

For immediate local tests on follow-ups, set `next_send_at = CURRENT_TIMESTAMP` on recipients (see [Local test send](#local-test-send-2-emails)).

---

## Production deployment

### Recommended layout

| Service | How to run |
|---------|------------|
| Redis | Managed Redis, or `docker-compose up -d redis` on a VM |
| FAPI backend | Existing production deploy (`wbl-backend`) |
| Celery worker | **2+ replicas**, `restart: always` |
| Scheduler | **1 instance only** (avoid duplicate runs) |

### Docker Compose (this repo)

```bash
docker-compose up -d redis
docker-compose up -d worker scheduler
```

Notes:

- `docker-compose.yml` sets worker `--concurrency=4` (Linux). On Windows hosts, run worker on Linux VM/container.
- Only **one** scheduler container — multiple schedulers can double-send.
- Mount `.env` and persist `./data/campaigns.duckdb` if using local DuckDB.

### Production `.env` differences

| Setting | Local | Production |
|---------|-------|--------------|
| `MAIN_API_BASE_URL` | `http://127.0.0.1:8000/api` | `https://your-api.domain/api` |
| `REDIS_URL` | `redis://localhost:6379/0` | Production Redis URL (TLS if required) |
| `LOG_LEVEL` | `DEBUG` | `INFO` |
| `USE_LOCAL_DUCKDB_CAMPAIGNS` | Often `true` for testing | Team choice; `false` uses remote `campaign_emails` only |

### Production checklist (daily)

- [ ] Redis reachable from worker and scheduler
- [ ] FAPI backend healthy (`/docs` or health endpoint)
- [ ] At least one Celery worker `ready` (monitor queue depth)
- [ ] Scheduler running (single instance)
- [ ] SMTP credentials valid (App Passwords, not expired)
- [ ] MySQL: schedules `enabled=1`
- [ ] Disk space for DuckDB / Parquet if analytics enabled

### systemd example (Linux VM)

**Scheduler** — one unit, `Restart=always`

**Worker** — one or more units, `Restart=always`

```ini
# Example — adjust paths
ExecStart=/path/to/venv/bin/python /path/to/Project-Weekly-Outreach/run_scheduler.py
WorkingDirectory=/path/to/Project-Weekly-Outreach
```

```ini
ExecStart=/path/to/venv/bin/celery -A app.workers.celery_app worker --loglevel=info --concurrency=4
```

---

## Pre-flight checklists

### Local — before every test run

- [ ] Docker Desktop running (if using Docker Redis)
- [ ] Port **6379** open (Redis)
- [ ] Port **8000** open (backend)
- [ ] `.\venv\Scripts\activate` and `pip install -r requirements.txt` done once
- [ ] Celery worker shows **`ready`**
- [ ] Schedule `next_run_at <= NOW()` (SQL above)
- [ ] For weekend test: set 2 recipients `next_send_at = CURRENT_TIMESTAMP` in DuckDB
- [ ] Gmail App Passwords in `email_smtp_credentials`

### Production — before enabling a new candidate

- [ ] `run_outreach_emails = 1` only when ready to send
- [ ] `run_parameters` has `candidate_id`, `candidate_name`, `linkedin_url`
- [ ] Vendor list populated in `outreach_email_recipients`
- [ ] SMTP accounts under daily limit (`daily_limit`, `current_day_sent`)
- [ ] Worker + scheduler healthy
- [ ] Suppression / bounce rules understood (`data/suppression_list.csv` on analytics runs)

---

## DuckDB maintenance (local mode)

Useful SQL when `USE_LOCAL_DUCKDB_CAMPAIGNS=true`. Database file: `./data/campaigns.duckdb`.

**Campaign overview:**

```sql
SELECT id, candidate_id, total_recipients, active_recipients, status
FROM campaigns ORDER BY id;
```

**Attempts by status:**

```sql
SELECT campaign_id, status, COUNT(*)
FROM campaign_email_attempts
GROUP BY 1, 2;
```

**Reset stuck claimed attempts:**

```sql
UPDATE campaign_email_attempts
SET status = 'pending', claimed_at = NULL, claimed_by = NULL
WHERE status = 'claimed';
```

**Force recipients due now (limit 2 for testing):**

```sql
UPDATE campaign_recipients
SET next_send_at = CURRENT_TIMESTAMP
WHERE id IN (
  SELECT id FROM campaign_recipients
  WHERE campaign_id = 3 AND status = 'active'
  ORDER BY id LIMIT 2
);
```

---

## How to verify emails sent

### 1. DuckDB (local mode)

```sql
SELECT id, campaign_id, vendor_email, status, sent_at
FROM campaign_email_attempts
WHERE status = 'sent'
ORDER BY sent_at DESC;
```

### 2. Celery worker log

Success:

```
smtp_email_sent
local_campaign_email_sent_successfully
Task send_outreach_email[...] succeeded
```

Failure:

```
smtp_send_error
local_campaign_transient_error
```

### 3. Gmail Sent folder

Check the **from** account used (`cloud.ipo11@gmail.com`, etc.).

### 4. MySQL run log

```sql
SELECT id, status, records_processed, error_summary, started_at, finished_at
FROM automation_workflow_logs
WHERE schedule_id = YOUR_SCHEDULE_ID
ORDER BY id DESC
LIMIT 5;
```

### 5. API — due schedules

```http
GET /api/orchestrator/schedules/due
Authorization: Bearer <token>
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `Connection refused` :8000 | Backend down | Start `uvicorn fapi.main:app` |
| `Connection refused` :6379 | Redis down | `docker-compose up -d redis` |
| `celery not recognized` | Deps not installed | `pip install -r requirements.txt` |
| `schedules_processed=0` | `next_run_at` in future | SQL: set `next_run_at = UTC_TIMESTAMP()` |
| `emails_enqueued=0`, `no_due_recipients` | Business hours / delay pending | Set `next_send_at = CURRENT_TIMESTAMP` in DuckDB for tests |
| Tasks enqueued, nothing sends | Worker not running | Start Celery **before** scheduler |
| Attempts stuck `claimed` | Worker crashed mid-run | Reset `claimed` → `pending` in DuckDB, restart worker |
| `534 WebLoginRequired` | Bad Gmail password | Use App Password in DB |
| `schedule_missing_candidate_id` | Bad schedule row | Fix `run_parameters` on that schedule |
| Duplicate sends | Multiple schedulers | Run only **one** scheduler instance |

### Decision flow

```
Scheduler runs?
  NO  → backend up? schedule due?
  YES → emails_enqueued > 0?
          NO  → recipients due? weekend? update next_send_at in DuckDB
          YES → worker ready?
                  NO  → start celery
                  YES → smtp_email_sent in logs?
                          NO  → fix Gmail App Password
                          YES → check Sent folder / DuckDB sent attempts
```

---

## Known limitations

1. **`reset_stale_claims()`** exists in code but is **not** called automatically — reset `claimed` rows in DuckDB manually if a worker crashes mid-send.
2. **Windows Celery** requires `--pool=solo` (no prefork).

---

## Quick reference — local test (copy/paste)

```powershell
# Terminal 1
cd Project-Weekly-Outreach
docker-compose up -d redis

# Terminal 2
cd ..\wbl-backend
uvicorn fapi.main:app --reload --host 127.0.0.1 --port 8000

# Terminal 3
cd Project-Weekly-Outreach
.\venv\Scripts\activate
celery -A app.workers.celery_app worker --loglevel=info --pool=solo

# Terminal 4 (after MySQL: next_run_at = UTC_TIMESTAMP() for your schedule)
# and DuckDB: set 2 recipients next_send_at = CURRENT_TIMESTAMP
.\venv\Scripts\activate
python run_scheduler.py --once
# wait 90-120 seconds, then query campaign_email_attempts for status = 'sent'
```

---

## Related docs

- `README.md` — project overview
- `QUICKSTART.md` — short setup
- `LOCAL_CAMPAIGNS_GUIDE.md` — DuckDB sequence details
- `EXECUTION_FLOW.md` — end-to-end flow with MySQL trigger

---

*Last updated from operational troubleshooting — include schedule id, candidate id, and SMTP provider when reporting issues.*
