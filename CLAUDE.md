# Email Outreach Service - Codebase Documentation

**Project:** Production-ready email outreach service (similar to Instantly)  
**Language:** Python 3.11+  
**Framework:** FastAPI + Celery + Redis  
**Architecture:** Headless orchestrator-driven campaign automation with REST API backend

---

## System Overview

This is a **headless email outreach worker** that operates without a local database. All campaign data, SMTP credentials, and execution state live in an external FastAPI backend (referred to as "fapi" or "MAIN_API"). The worker:

1. **Polls** the orchestrator API for due campaigns (`/orchestrator/schedules/due`)
2. **Enqueues** email tasks into Celery with staggered delays
3. **Sends** emails via Gmail API or SMTP with automatic account rotation
4. **Classifies** bounces (hard/soft/invalid) and updates status via API webhooks
5. **Generates** nightly analytics reports using DuckDB (Parquet export pipeline)

**Key principle:** Zero local SQL. All persistence happens via REST API calls to the external backend.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Task Queue | Celery 5.4+ |
| Message Broker | Redis 6+ |
| Email Sending | Gmail API + aiosmtplib (SMTP) |
| Template Engine | Jinja2 |
| HTTP Client | httpx (sync) |
| Analytics | DuckDB + pandas + pyarrow |
| Logging | structlog (JSON logs) |
| Config | pydantic-settings (.env) |

---

## Project Structure

```
/
├── app/
│   ├── analytics/              # DuckDB analytics pipeline
│   │   ├── exporter.py         # Fetches campaign data → Parquet
│   │   └── queries.py          # Bounce reports, suppression list
│   ├── core/                   # Shared utilities
│   │   ├── auth.py             # TokenManager (auto-refresh on 401)
│   │   ├── config.py           # Settings from .env
│   │   ├── logging.py          # structlog setup
│   │   └── redis_client.py     # Redis helpers (metadata, counters)
│   ├── integrations/           # External service clients
│   │   ├── gmail_client.py     # Gmail API wrapper
│   │   └── smtp_client.py      # SMTP client (TLS)
│   ├── scheduler/
│   │   └── campaign_scheduler.py  # Main polling loop
│   ├── services/
│   │   ├── email_service.py    # Email sending logic
│   │   └── report_service.py   # HTML report generation
│   └── workers/
│       ├── celery_app.py       # Celery config
│       └── email_worker.py     # Task handler (retries, bounce classification)
├── run_scheduler.py            # Entry point: scheduler + daily reports
├── run_analytics.py            # Entry point: DuckDB reports
├── run_daily_report.py         # Standalone daily report runner
├── data/analytics/             # Parquet files + suppression_list.csv (auto-created)
├── requirements.txt
├── pyproject.toml              # Black/isort/mypy config
├── Makefile                    # Dev commands
└── .env.example
```

---

## Key Components

### 1. Scheduler (`app/scheduler/campaign_scheduler.py`)

**Responsibility:** Poll the orchestrator API and enqueue Celery tasks.

**Dual-mode operation:**
- **Remote mode** (default): Traditional single-send flow via remote API
- **Local mode** (new): 4-step weekly sequence via local DuckDB

**Remote flow per run (every 60s by default):**

1. `GET /orchestrator/schedules/due` → list of due campaigns
2. For each schedule:
   - `POST /orchestrator/schedules/{id}/lock` → acquire lock
   - `GET /orchestrator/smtp-credentials/active` → fetch SMTP accounts
   - Calculate daily send limits (respects warmup limits)
   - `POST /orchestrator/schedules/{id}/snapshot` → create campaign_emails snapshot
   - `GET /orchestrator/schedules/{id}/pending-emails?limit=N` → fetch emails to send
   - Enqueue Celery tasks with randomized delays (30-120s)
   - Store run metadata in Redis for reporting

**Local DuckDB flow (when `USE_LOCAL_DUCKDB_CAMPAIGNS=true`):**

1. `GET /orchestrator/schedules/due` → list of due campaigns
2. For each schedule:
   - `POST /orchestrator/schedules/{id}/lock` → acquire lock
   - Create/resume local campaign in DuckDB
   - Enroll recipients from remote `/orchestrator/candidates/{id}/outreach-emails`
   - Create 4-step weekly sequence (0d, 3d, 5d, 7d business days)
   - Write `local_campaign_id` to remote `schedule.run_parameters`
   - Generate pending attempts for recipients with `next_send_at <= NOW()`
   - Claim attempts atomically (up to daily limit)
   - Enqueue Celery tasks with `mode='local_duckdb_campaign'`

**Weekday-only:** Scheduler only runs Mon-Fri (configured via cron or internal check).

**Daily reports:** At 6 PM, sends HTML summary of the day's campaigns via SMTP (includes both remote and local metrics).

---

### 2. Email Worker (`app/workers/email_worker.py`)

**Responsibility:** Process enqueued email tasks and update status via API or local DuckDB.

**Dual-mode detection:** Worker inspects `payload.get("mode")` to determine execution path:
- **"remote"** (default): Update remote API (existing flow)
- **"local_duckdb_campaign"**: Update local DuckDB tables (new flow)

**Remote task flow:**

1. Receive payload: `{campaign_email_id, to_email, from_email, smtp_config, template_data, ...}`
2. Validate email format → if invalid, mark as `bounced:invalid`
3. Render template (Jinja2) with personalization variables
4. Send via SMTP or Gmail API
5. On success:
   - `PUT /campaign-emails/{id}` → status='sent'
   - Increment Redis counter for run report
6. On failure:
   - Classify bounce type (hard/soft/invalid)
   - `PUT /campaign-emails/{id}` → status='bounced', bounce_type=...
   - Retry up to 3x for soft bounces (5-min delays)
7. If last task in run → trigger HTML report

**Local DuckDB task flow:**

1. Receive payload: `{mode='local_duckdb_campaign', attempt_id, campaign_id, recipient_id, ...}`
2. Validate email format → if invalid, mark attempt `bounced:invalid`, mark recipient bounced
3. Render template (Jinja2) with personalization variables
4. Send via SMTP or Gmail API (reuses existing EmailService)
5. On success:
   - UPDATE `campaign_email_attempts` → status='sent'
   - Advance recipient to next step (or mark completed if step 4)
   - Update `campaign_daily_metrics` → sent++
6. On failure:
   - Classify bounce type (hard/soft/invalid)
   - Hard/invalid: mark recipient `status='bounced'`, stop all future sends
   - Soft: keep recipient `status='active'`, allow retry via Celery
   - Update `campaign_daily_metrics` → bounced++
7. Increment SMTP credential counter (same as remote)
8. If last task in run → trigger HTML report (same Redis coordination)

**Bounce classification:**

| Error Type | bounce_type | Action |
|---|---|---|
| Invalid email format | `invalid` | Never retry, add to suppression |
| SMTP 5xx (550-554) | `hard` | Never retry, add to suppression |
| SMTP 4xx (temp failure) | `soft` | Retry up to 3x |

---

### 3. Email Service (`app/services/email_service.py`)

**Responsibility:** Abstraction layer for Gmail API vs SMTP sending.

- Auto-selects transport based on `smtp_config.auth_type` (gmail/smtp)
- Handles OAuth token refresh for Gmail API
- Raises `SMTPPermanentError` or `SMTPTransientError` for bounce classification

---

### 4. Local Campaign Service (`app/services/local_campaign_service.py`)

**Responsibility:** Core orchestration for local DuckDB campaign execution (new feature).

**Key methods:**
- `create_or_resume_campaign()` — Create new or resume existing campaign (idempotent)
- `create_default_steps()` — Create 4-step weekly sequence (0d, 3d, 5d, 7d)
- `enroll_recipients()` — Copy contacts from remote API to local `campaign_recipients` table
- `generate_due_attempts()` — Create pending attempts for recipients with `next_send_at <= NOW()`
- `claim_attempts()` — Atomically claim pending attempts (ROWID-based, DuckDB-safe)
- `advance_recipient()` — Move recipient to next step or mark completed
- `mark_recipient_bounced()` — Stop recipient on hard/invalid bounce, allow retry on soft
- `update_daily_metrics()` — Increment daily metrics (sent/failed/bounced)
- `update_remote_run_parameters()` — Write `local_campaign_id` back to remote schedule

**Local DuckDB Schema:**
- `campaigns` — Campaign metadata, links to remote schedule
- `campaign_steps` — 4-step sequence definition
- `campaign_recipients` — Enrolled contacts with progression tracking (`current_step_number`, `next_send_at`)
- `campaign_email_attempts` — Individual send attempts (like remote `campaign_emails`)
- `campaign_daily_metrics` — Aggregated daily stats per campaign

**Business day logic:** Uses `app/utils/business_days.py` for weekend skipping, 9 AM - 5 PM send window enforcement, and 30-120s jitter.

### 5. Analytics (`app/analytics/`)

**Responsibility:** Export campaign data to Parquet and generate reports.

**Usage:**

```bash
# Fetch all data from API → save to Parquet → print reports
python run_analytics.py

# Filter to one candidate
python run_analytics.py --candidate-id 570

# Skip API call, use existing Parquet files
python run_analytics.py --report-only
```

**Reports generated:**

- Bounce summary (hard/soft/invalid per candidate)
- Campaign progress (sent/failed/pending per candidate)
- SMTP account health (delivery rate per account)
- Daily send volume (emails per account per day)
- Suppression list (hard + invalid → CSV)

**Output:** `./data/suppression_list.csv` — never contact these emails again.

---

## Local DuckDB Campaign Engine (New Feature)

### Overview

The local DuckDB campaign engine enables Instantly-like multi-step email sequences with automatic progression, reducing API dependency by ~80%.

**Key features:**
- **4-step weekly sequence**: Immediate, +3d, +5d, +7d (business days only)
- **Automatic progression**: Recipients advance through steps on successful sends
- **Smart bounce handling**: Hard/invalid bounces stop sends, soft bounces allow retries
- **Business hours enforcement**: 9 AM - 5 PM send window with weekend skipping
- **Local execution**: Campaign state stored in DuckDB, minimal API calls
- **Backward compatible**: Feature flag preserves existing remote flow

### Enabling Local Campaigns

Add to `.env`:
```bash
USE_LOCAL_DUCKDB_CAMPAIGNS=true
DUCKDB_CAMPAIGN_PATH=./data/campaigns.duckdb
```

Restart scheduler. Next campaign activation will use local DuckDB flow.

### How It Works

1. **Activation**: Set `candidate_marketing.run_outreach_emails = 1` (remote DB)
2. **Campaign creation**: Scheduler creates local campaign in DuckDB
3. **Enrollment**: Contacts copied from remote `outreach_emails` → local `campaign_recipients`
4. **Sequence generation**: 4 steps created automatically (0d, 3d, 5d, 7d delays)
5. **Step 1**: Emails sent immediately
6. **Progression**: Successful sends → advance to next step (calculate `next_send_at` with business day logic)
7. **Step 2-4**: Emails sent when `next_send_at <= NOW()`
8. **Completion**: All recipients finish or bounce → campaign marked completed

**Bounce handling:**
- Hard/invalid: Stop all future sends for recipient
- Soft: Allow retry (up to 3x with 5-min delays)

**Remote sync:**
- `local_campaign_id` written to remote `schedule.run_parameters`
- Campaign completion updates remote schedule
- Daily metrics synced to remote workflow logs

### Documentation

See `LOCAL_CAMPAIGNS_GUIDE.md` for detailed user guide including:
- Campaign lifecycle
- Database schema
- Monitoring & troubleshooting
- Performance metrics
- Migration path

---

## Configuration

### Environment Variables (`.env`)

| Variable | Description | Default |
|---|---|---|
| `MAIN_API_BASE_URL` | External FastAPI backend URL | `http://localhost:8000` |
| `API_BEARER_TOKEN` | Auth token for API | (required) |
| `API_LOGIN_EMAIL` | Auto-refresh token on 401 | (optional) |
| `API_LOGIN_PASSWORD` | Auto-refresh token on 401 | (optional) |
| `REDIS_URL` | Redis connection string | `redis://localhost:6379/0` |
| `SCHEDULER_INTERVAL_SECONDS` | How often scheduler runs | `60` |
| `MIN_DELAY_SECONDS` | Min delay between emails | `30` |
| `MAX_DELAY_SECONDS` | Max delay between emails | `120` |
| `REPORT_FROM_EMAIL` | SMTP account for reports | (required) |
| `REPORT_FROM_PASSWORD` | App password for reports | (required) |
| `REPORT_RECIPIENT_EMAIL` | Admin inbox for reports | (required) |
| `GOOGLE_CLIENT_ID` | Gmail API OAuth (optional) | - |
| `GOOGLE_CLIENT_SECRET` | Gmail API OAuth (optional) | - |
| `USE_LOCAL_DUCKDB_CAMPAIGNS` | Enable local DuckDB campaigns | `false` |
| `DUCKDB_CAMPAIGN_PATH` | Path to DuckDB campaign file | `./data/campaigns.duckdb` |

---

## API Contracts (External Backend)

### Orchestrator Endpoints

**Poll for due schedules:**

```
GET /orchestrator/schedules/due
→ [{id, automation_workflow_id, run_parameters: {candidate_id}, ...}]
```

**Lock a schedule:**

```
POST /orchestrator/schedules/{id}/lock
→ {success: true}
```

**Fetch active SMTP credentials:**

```
GET /orchestrator/smtp-credentials/active
→ [{id, smtp_email, smtp_password, daily_limit, warmup_daily_limit, is_warming_up, ...}]
```

**Create campaign snapshot:**

```
POST /orchestrator/schedules/{id}/snapshot
→ {snapshot_id, total_emails}
```

**Fetch pending emails:**

```
GET /orchestrator/schedules/{id}/pending-emails?limit=N
→ [{campaign_email_id, vendor_email, first_name, company, ...}]
```

**Update email status:**

```
PUT /campaign-emails/{id}
Body: {status: 'sent'|'bounced'|'failed', bounce_type, error_message, sent_at, ...}
→ {success: true}
```

---

## Development

### Setup

```bash
# Install dependencies
make install

# Copy environment template
cp .env.example .env
# Edit .env with your API credentials

# Start Redis
make start-deps
```

### Running Locally

**Terminal 1 - Celery Worker:**

```bash
make worker
# or: celery -A app.workers.celery_app worker --loglevel=info --concurrency=4
```

**Terminal 2 - Scheduler:**

```bash
make scheduler
# or: python run_scheduler.py
```

**One-time run (for testing):**

```bash
make scheduler-once
# or: python run_scheduler.py --once
```

### Monitoring

**Celery Flower (web UI):**

```bash
make flower
# → http://localhost:5555
```

**Check active tasks:**

```bash
celery -A app.workers.celery_app inspect active
celery -A app.workers.celery_app inspect stats
```

---

## Production Deployment

### Systemd Service (Linux)

**Scheduler:**

```ini
[Unit]
Description=Email Outreach Scheduler
After=network.target redis.service

[Service]
Type=simple
WorkingDirectory=/path/to/project
Environment="PATH=/path/to/venv/bin"
ExecStart=/path/to/venv/bin/python run_scheduler.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Celery Worker:**

```ini
[Unit]
Description=Email Outreach Celery Worker
After=network.target redis.service

[Service]
Type=simple
WorkingDirectory=/path/to/project
Environment="PATH=/path/to/venv/bin"
ExecStart=/path/to/venv/bin/celery -A app.workers.celery_app worker --loglevel=info --concurrency=10 --max-tasks-per-child=1000 --time-limit=300
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Enable:**

```bash
sudo systemctl enable email-scheduler email-worker
sudo systemctl start email-scheduler email-worker
```

### Docker Compose (Production)

See `docker-compose.yml` — includes Redis + Celery worker + scheduler as separate services.

```bash
docker-compose up -d
```

---

## Common Workflows

### Adding a new template variable

1. **Backend:** Add column to `campaign_emails` or related table
2. **Backend:** Include new field in `/orchestrator/schedules/{id}/pending-emails` response
3. **Worker:** No code change needed — Jinja2 auto-ignores unknown variables
4. **Template:** Use `{{new_field}}` in email body

### Adding a new SMTP account

1. **Backend:** Insert into `smtp_credentials` table with `is_active=1`
2. **Scheduler:** Auto-fetches on next run via `/orchestrator/smtp-credentials/active`
3. **Worker:** Auto-rotates to new account

### Debugging a bounce

1. **Check logs:** `journalctl -u email-worker -f` → search for `worker_permanent_bounce`
2. **Check API:** `GET /campaign-emails/{id}` → see `bounce_type`, `error_message`
3. **Check suppression list:** `cat ./data/suppression_list.csv | grep email@example.com`

### Running analytics for a specific candidate

```bash
python run_analytics.py --candidate-id 570
```

### Testing email sending without scheduler

```python
from app.workers.email_worker import send_outreach_email

send_outreach_email.delay({
    "campaign_email_id": 123,
    "to_email": "test@example.com",
    "from_email": "sender@yourdomain.com",
    "smtp_config": {...},
    "template_data": {"first_name": "John", ...},
    ...
})
```

---

## Code Style & Linting

**Configured in `pyproject.toml`:**

- **Black:** line-length=100, target-version=py311
- **isort:** profile=black, multi_line_output=3
- **mypy:** strict typing, disallow_untyped_defs

**Run linters:**

```bash
black app/
isort app/
mypy app/
```

---

## Testing

**Framework:** pytest + pytest-asyncio

```bash
make test
# or: pytest --cov=app --cov-report=term-missing
```

**Test structure:**

```
tests/
├── test_scheduler.py      # Scheduler logic
├── test_email_worker.py   # Worker bounce classification
└── test_email_service.py  # SMTP/Gmail integration
```

---

## Troubleshooting

### "No emails being sent"

1. **Check scheduler is running:**
   ```bash
   ps aux | grep run_scheduler
   systemctl status email-scheduler
   ```

2. **Check Celery workers:**
   ```bash
   celery -A app.workers.celery_app inspect active
   ```

3. **Check API connectivity:**
   ```bash
   curl -H "Authorization: Bearer $API_BEARER_TOKEN" \
     $MAIN_API_BASE_URL/orchestrator/schedules/due
   ```

4. **Check Redis:**
   ```bash
   redis-cli ping
   ```

### "Gmail API errors"

- Verify `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set
- Check OAuth scopes include `https://www.googleapis.com/auth/gmail.send`
- Ensure refresh token is valid (check API logs for `401` errors)
- Enable Gmail API in Google Cloud Console

### "SMTP authentication failed"

- Use App Password, not regular password (for Gmail)
- Check firewall allows outbound port 587 (TLS) or 465 (SSL)
- Verify credentials are correct in the external API's `smtp_credentials` table

### "Analytics shows no data"

- Ensure at least one campaign has `status='sent'` in the API database
- Check `MAIN_API_BASE_URL` and `API_BEARER_TOKEN` in `.env`
- Run `python run_analytics.py --report-only` to use cached Parquet files

### "Worker retries exhausted"

- Check `error_message` in API: `GET /campaign-emails/{id}`
- If soft bounce (4xx), increase retry limit in `email_worker.py`:
  ```python
  @celery_app.task(bind=True, max_retries=5, default_retry_delay=300)
  ```

---

## Security Considerations

1. **API tokens:** Store `API_BEARER_TOKEN` securely (never commit to git)
2. **SMTP credentials:** Encrypted at rest in external backend database
3. **Redis:** Use password auth in production (`redis://:password@host:6379/0`)
4. **Logs:** Sensitive data (passwords, tokens) redacted via structlog processors
5. **Rate limiting:** Enforced per-account to avoid IP blacklisting

---

## Performance & Scalability

- **Horizontal scaling:** Run multiple Celery workers (no shared state)
- **Concurrency:** Default=4, increase to 10+ for high-volume campaigns
- **Rate limiting:** Respects per-account daily limits + warmup limits
- **Staggered delays:** Random 30-120s delays to avoid spam detection
- **Redis caching:** Token manager caches bearer token (TTL=3600s)

---

## Future Enhancements

- [ ] Support for Outlook/Exchange SMTP
- [ ] A/B testing for subject lines
- [ ] Click tracking via custom link wrapper
- [ ] Open tracking via 1x1 pixel
- [ ] Webhook for real-time bounce notifications
- [ ] Multi-region SMTP proxies for IP rotation

---

## Key Files Reference

| File | Purpose |
|---|---|
| `run_scheduler.py` | Scheduler entry point |
| `run_analytics.py` | Analytics/reporting entry point |
| `app/scheduler/campaign_scheduler.py` | Main polling loop |
| `app/workers/email_worker.py` | Celery task handler |
| `app/services/email_service.py` | SMTP/Gmail abstraction |
| `app/core/auth.py` | TokenManager (auto-refresh) |
| `app/core/config.py` | Settings from .env |
| `app/analytics/exporter.py` | Parquet export pipeline |
| `app/analytics/queries.py` | DuckDB reports |
| `Makefile` | Dev commands |

---

## Contact & Support

- **Issues:** Report bugs in the main backend repository (not this worker)
- **Logs:** Check `journalctl -u email-scheduler -u email-worker -f`
- **Monitoring:** Use Flower for Celery task visibility

---

**Last Updated:** 2026-05-18  
**Maintained by:** Sampath Velupula
