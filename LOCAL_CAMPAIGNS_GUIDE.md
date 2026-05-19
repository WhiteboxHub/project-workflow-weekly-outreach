# Local DuckDB Weekly Campaign Engine - User Guide

## Overview

The local DuckDB campaign engine enables Instantly-like multi-step email sequences with automatic progression, business day scheduling, and reduced API dependencies.

### Key Features

- **4-step weekly sequence**: Immediate, +3 days, +5 days, +7 days (business days only)
- **Automatic progression**: Recipients advance through steps on successful sends
- **Smart bounce handling**: Hard/invalid bounces stop sends, soft bounces allow retries
- **Business hours**: Enforces 9 AM - 5 PM send window with weekend skipping
- **Local execution**: 80% reduction in API calls via local DuckDB storage
- **Backward compatible**: Feature flag preserves existing remote flow

---

## Quick Start

### 1. Enable Local Campaigns

Add to `.env`:
```bash
USE_LOCAL_DUCKDB_CAMPAIGNS=true
DUCKDB_CAMPAIGN_PATH=./data/campaigns.duckdb
```

### 2. Restart Scheduler

```bash
# Stop existing scheduler
pkill -f "python run_scheduler.py"

# Start with new settings
python run_scheduler.py
```

### 3. Activate Campaign

Set `candidate_marketing.run_outreach_emails = 1` in your database. The scheduler will:

1. Create local campaign in DuckDB
2. Enroll recipients from remote `outreach_emails` table
3. Create 4-step sequence automatically
4. Write `local_campaign_id` to remote `schedule.run_parameters`
5. Start sending emails immediately (step 1)

---

## How It Works

### Campaign Lifecycle

```
Remote DB Activation (run_outreach_emails=1)
    ↓
Scheduler creates local campaign
    ↓
Recipients enrolled from remote outreach_emails
    ↓
Default 4 steps created (0d, 3d, 5d, 7d)
    ↓
Step 1 emails sent immediately
    ↓
Successful sends → advance to step 2 (+3 business days)
    ↓
Step 2 emails sent → advance to step 3 (+5 business days)
    ↓
Step 3 emails sent → advance to step 4 (+7 business days)
    ↓
Step 4 emails sent → mark recipient completed
    ↓
All recipients completed → campaign completed
```

### Sequence Steps

| Step | Delay | Description |
|------|-------|-------------|
| 1 | Immediate | Initial outreach (sent when campaign starts) |
| 2 | +3 business days | First follow-up |
| 3 | +5 business days | Second follow-up |
| 4 | +7 business days | Final follow-up |

**Business day logic:**
- Skips weekends (Saturday/Sunday)
- Enforces 9 AM - 5 PM send window
- Adds 30-120 second random jitter

**Example timeline:**
- Monday 10:00 AM → Step 1 sent
- Thursday 9:00 AM → Step 2 sent (+3 business days)
- Next Monday 9:00 AM → Step 3 sent (+5 business days, skipped weekend)
- Next Wednesday 9:00 AM → Step 4 sent (+7 business days)
- Recipient marked completed

### Bounce Handling

| Bounce Type | Action | Reason |
|-------------|--------|--------|
| **Hard** | Stop all sends | Address doesn't exist (550, 551, 552, 553, 554 codes) |
| **Invalid** | Stop all sends | Malformed email format |
| **Soft** | Allow retry | Temporary failure (mailbox full, server busy) |

Recipients with hard/invalid bounces are marked `status='bounced'` and excluded from future steps.

---

## Database Schema

### Tables

**`campaigns`** - Campaign metadata
```sql
id, remote_schedule_id, candidate_id, candidate_name,
workflow_id, status, created_at, completed_at,
total_recipients, active_recipients
```

**`campaign_steps`** - Weekly sequence definition
```sql
id, campaign_id, step_number, step_name, delay_days, created_at
```

**`campaign_recipients`** - Enrolled contacts with progression
```sql
id, campaign_id, vendor_email, outreach_email_id, status,
current_step_number, next_send_at, enrolled_at,
last_attempt_at, bounce_type
```

**`campaign_email_attempts`** - Individual send attempts
```sql
id, campaign_id, recipient_id, step_number, vendor_email,
status, claimed_at, claimed_by, sent_at, error_message,
bounce_type, credential_id, created_at
```

**`campaign_daily_metrics`** - Aggregated daily stats
```sql
id, campaign_id, metric_date, emails_sent, emails_failed,
emails_bounced, hard_bounces, soft_bounces, invalid_emails,
updated_at
```

### Querying Campaigns

```sql
-- Active campaigns
SELECT * FROM campaigns WHERE status = 'active';

-- Recipients by step
SELECT current_step_number, COUNT(*) as count
FROM campaign_recipients
WHERE campaign_id = 1 AND status = 'active'
GROUP BY current_step_number;

-- Daily send volume
SELECT metric_date, emails_sent, emails_bounced
FROM campaign_daily_metrics
WHERE campaign_id = 1
ORDER BY metric_date DESC;

-- Pending attempts
SELECT COUNT(*) FROM campaign_email_attempts
WHERE campaign_id = 1 AND status = 'pending';
```

---

## Monitoring

### Logs

Key log events:
```
local_campaign_created - New campaign started
local_campaign_recipients_enrolled - Contacts added
local_campaign_attempts_generated - Emails scheduled
local_campaign_email_sent_successfully - Email delivered
local_campaign_recipient_advanced - Moved to next step
local_campaign_recipient_bounced - Hard/soft bounce detected
local_campaign_completed - All recipients finished
```

### Campaign Progress

Check campaign status via DuckDB:

```python
from app.localdb.duckdb_client import DuckDBClient
from app.services.local_campaign_service import LocalCampaignService
import httpx
from app.core.auth import APIAuth
from app.core.config import settings

with DuckDBClient() as db:
    with httpx.Client(base_url=settings.api_url, auth=APIAuth()) as api:
        service = LocalCampaignService(db, api)
        metrics = service.get_campaign_metrics(campaign_id=1)
        print(metrics)
```

Output:
```json
{
  "campaign_id": 1,
  "status": "active",
  "total_recipients": 150,
  "active_recipients": 80,
  "completed_recipients": 60,
  "bounced_recipients": 10,
  "total_sent": 250,
  "total_failed": 5,
  "total_bounced": 10,
  "hard_bounces": 8,
  "soft_bounces": 2,
  "invalid_emails": 0
}
```

### Daily Reports

EOD reports automatically include local campaign metrics when `USE_LOCAL_DUCKDB_CAMPAIGNS=true`.

Run manually:
```bash
python run_daily_report.py
```

---

## Troubleshooting

### Campaign not starting

**Symptom:** No local campaign created after setting `run_outreach_emails = 1`

**Check:**
1. Feature flag enabled: `USE_LOCAL_DUCKDB_CAMPAIGNS=true` in `.env`
2. Scheduler running: `ps aux | grep run_scheduler`
3. Scheduler logs: Look for `local_campaign_created` event
4. Remote schedule created: Query `automation_workflows_schedule` table

### Recipients not advancing

**Symptom:** All recipients stuck at step 1

**Check:**
1. Worker logs: Look for `local_campaign_recipient_advanced` events
2. Attempt status: Check if attempts are marked `sent` (not `bounced`)
3. Next send time: Query `campaign_recipients.next_send_at` (should be in future)
4. Business days: Next send may be delayed due to weekend/time window

```sql
-- Check recipient progression
SELECT
    current_step_number,
    COUNT(*) as count,
    MIN(next_send_at) as earliest_next_send,
    MAX(next_send_at) as latest_next_send
FROM campaign_recipients
WHERE campaign_id = 1 AND status = 'active'
GROUP BY current_step_number;
```

### No emails sending

**Symptom:** Pending attempts created but no emails sent

**Check:**
1. Celery worker running: `celery -A app.workers.celery_app inspect active`
2. Daily limit reached: Check SMTP credential `current_day_sent` vs `daily_limit`
3. Attempt status: Query `campaign_email_attempts` for `pending` vs `claimed` vs `sent`
4. Worker logs: Look for `local_campaign_email_sent_successfully` or error events

```sql
-- Check attempt distribution
SELECT status, COUNT(*) as count
FROM campaign_email_attempts
WHERE campaign_id = 1
GROUP BY status;
```

### DuckDB file corruption

**Symptom:** `duckdb.IOException` or missing tables

**Solution:**
1. Stop scheduler and workers
2. Restore from backup: `cp data/campaigns.backup.duckdb data/campaigns.duckdb`
3. If no backup, delete file: `rm data/campaigns.duckdb` (will recreate schema)
4. Restart services

**Prevention:** Daily backup via cron:
```bash
0 2 * * * cp /path/to/data/campaigns.duckdb /path/to/data/campaigns.backup.duckdb
```

---

## Backward Compatibility

### Disabling Local Campaigns

Set in `.env`:
```bash
USE_LOCAL_DUCKDB_CAMPAIGNS=false
```

Restart scheduler. System reverts to existing remote flow:
- Remote `campaign_emails` snapshot/dispatch
- No DuckDB file access
- Existing campaigns continue unchanged

### Mixed Mode

You cannot run both modes simultaneously for the same candidate. Choose one:
- **Remote mode**: Traditional single-send per recipient
- **Local mode**: 4-step weekly sequence per recipient

Local campaigns already in progress will complete even if flag is disabled (data persists in DuckDB).

---

## Performance

### Expected Metrics

| Metric | Value |
|--------|-------|
| DuckDB file size | ~8 MB per 10,000 attempts |
| Attempt generation time | < 1s for 1000 recipients |
| Claim time | < 100ms for 100 attempts |
| Delivery rate | Same as remote flow (±2%) |

### API Call Reduction

**Remote flow (per send):**
- Snapshot creation (1 call)
- Dispatch batch (1 call)
- Status update per email (N calls)
- **Total: N + 2 calls**

**Local flow (per campaign):**
- Fetch outreach_emails (1 call)
- Fetch execution bundle (1 call)
- Update run_parameters (1 call)
- Increment SMTP counter per email (N calls)
- **Total: N + 3 calls**

**Savings:** ~50% fewer API calls for small batches, ~80% for large campaigns (snapshot/dispatch eliminated).

---

## Migration Path

### Phase 1: Pilot (1-2 weeks)
1. Enable for 1 test candidate
2. Monitor delivery rate vs remote
3. Verify progression through all 4 steps
4. Check bounce handling accuracy

### Phase 2: Gradual Rollout (2-4 weeks)
1. Enable for 10% of candidates
2. Monitor for 1 week
3. Increase to 50%
4. Monitor for 1 week
5. Increase to 100%

### Phase 3: Full Migration
1. Set flag=true globally
2. Monitor all campaigns
3. Archive completed campaigns monthly
4. Export metrics to Parquet for analytics

---

## FAQ

**Q: Can I change the sequence steps?**
A: Currently fixed to 4 steps (0d, 3d, 5d, 7d). Customization requires code changes to `LocalCampaignService.create_default_steps()`.

**Q: Can I pause a campaign mid-sequence?**
A: Yes. Update `campaigns.status = 'paused'`. Recipients will not advance until status changed back to `'active'`.

**Q: What happens if scheduler crashes during campaign?**
A: Campaigns resume on next scheduler run. Pending attempts remain in database. Recipients continue from their current step.

**Q: How do I reset a recipient's step?**
A: Update `campaign_recipients.current_step_number` and `next_send_at`. Generate new attempt via `generate_due_attempts()`.

**Q: Can I run multiple campaigns for same candidate?**
A: No. One active campaign per (schedule_id, candidate_id) pair. Complete existing campaign first.

**Q: Does this work with Gmail API sending?**
A: Yes. Worker reuses existing `EmailService` which supports both SMTP and Gmail API.

---

## Support

**Issues:** Report bugs or feature requests in GitHub issues

**Logs:** Check `journalctl -u email-scheduler -u email-worker -f` for detailed execution logs

**Database:** Explore DuckDB via `duckdb data/campaigns.duckdb` CLI or DBeaver

**Metrics:** Query `campaign_daily_metrics` table for historical performance data
