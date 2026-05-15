# Quick Start Guide

Get the email outreach service running in 5 minutes.

## Prerequisites

- Python 3.11+
- Application `fapi` Server Running (for the API backend)
- Docker & Docker Compose (for Redis)

## Setup

### 1. Install Dependencies

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install packages
make install
# or: pip install -r requirements.txt
```

### 2. Configure Environment

```bash
# Copy environment template
cp .env.example .env

# Edit .env with your settings
# At minimum, update these:
# - MAIN_API_BASE_URL (URL to your running fapi backend, e.g., http://localhost:8000)
# - API_BEARER_TOKEN (for secure webhook authentication)
# - REDIS_URL
# - SECRET_KEY
# - GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (for Gmail)
```

### 3. Start Infrastructure

Because the outreach worker operates natively over REST APIs, you no longer need PostgreSQL in docker. You only need Redis.

```bash
# Start Redis
make start-deps
# or: docker-compose up -d redis

# Wait a few seconds for services to be ready
```

### 4. Create Database Schema

The database relies exclusively on your FAPI server backend. No local database creation is necessary in this repository.
Instead, ensure your FAPI server has executed its migrations using your native backend deployment stack.

```bash
# Example inside your external FAPI backend project:
# alembic upgrade head
```

### 5. Load Sample Data (Optional)

You can load sample targets or mock credentials dynamically directly into your FAPI database. The Outreach engine will automatically parse them seamlessly.

### 6. Start Services

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

### 7. Run Analytics (After Campaign Sends)

Once emails have been sent, export campaign data and view bounce reports:

```bash
python run_analytics.py
```

This will:
- Fetch all completed email records from the API
- Save a Parquet file to `./data/analytics/`
- Print bounce summary, SMTP health, and suppression list to the console
- Write `./data/suppression_list.csv` (hard bounces + invalid emails)

## Verify It's Working

### Check Scheduler Logs

You should see output like:

```
scheduler_run_starting
polling_orchestrator endpoint="http://localhost:8000/orchestrator/schedules/due"
emails_enqueued count=3 target="vendor_email"
scheduler_run_completed
```

### Check Worker Logs

You should see:

```
email_worker_started workflow_id=1
worker_received_payload target="john.doe@acme.com"
email_sent_successfully
executing_reset_webhook endpoint="execute-reset-sql" status=200
```

For bounced emails you'll see one of:

```
worker_invalid_email vendor_email="bad-email"
worker_permanent_bounce bounce_type="hard" error="550 no such user"
worker_transient_error error="Connection timed out"  # retried up to 3x
```

### Check Database

```sql
-- Check campaign email status and bounce types
SELECT status, bounce_type, COUNT(*) AS count
FROM campaign_emails
WHERE candidate_id = 570
GROUP BY status, bounce_type
ORDER BY status;

-- View suppression list (never contact again)
SELECT vendor_email, bounce_type, error_message
FROM campaign_emails
WHERE bounce_type IN ('hard', 'invalid')
ORDER BY last_attempt_at DESC
LIMIT 10;
```

## Common Issues

### "No emails being sent"

1. **Check FAPI Endpoints are active:**
   Verify `MAIN_API_BASE_URL` exactly matches the running FAPI application's active domain. Make sure the API server is physically reachable over the network boundary.

2. **Check Authentication Tokens:**
   Ensure `API_BEARER_TOKEN` exactly matches the authorization constraint block built inside the backend FastAPI routes.

3. **Check Workflow Config:**
   The `fapi` `parameters_config` must possess valid dynamic keys (such as `recipient_update_sql`) for the celery worker to properly hook the native webhook deductions implicitly.

### "Gmail API errors"

- Verify OAuth2 credentials in `.env`
- Ensure refresh token is valid
- Check Gmail API is enabled in Google Cloud Console
- Verify scopes include `https://www.googleapis.com/auth/gmail.send`

### "SMTP authentication failed"

- Use App Password, not regular Gmail password
- Enable "Less secure app access" if using regular SMTP
- Check firewall allows outbound port 587/465

### "Analytics shows no data"

- Ensure at least one campaign has run and emails have `status = 'sent' / 'bounced' / 'failed'`
- Check `MAIN_API_BASE_URL` and `API_BEARER_TOKEN` are set in `.env`
- Run `python run_analytics.py --report-only` if you already have Parquet files
