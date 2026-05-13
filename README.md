# Email Outreach Service

A production-ready email outreach service similar to Instantly, built with Python, FastAPI, and Celery, adapted dynamically for Headless Orchestrator webhook APIs.

## Features

- **Multi-account Email Sending**: Support for Gmail API and SMTP
- **Campaign Management**: Headless multi-step workflows driven completely by API pipelines.
- **Smart Scheduling**: Mathematically parallel scheduling parsing JSON queues.
- **Account Rotation**: Dynamically pulls active identities securely from FAPI server lists.
- **Rate Limiting**: Custom execution rules handled completely natively.
- **Template Variables**: Personalize emails smoothly using Jinja2 templates via dynamic JSON arguments.
- **Tracking**: Webhook-based execution sync updates back to Master automation logs remotely.

## Architecture

### Core Components

1. **Scheduler Service** (`app/scheduler/campaign_scheduler.py`)
   - Runs every minute (configurable)
   - Fetches FAPI endpoint triggers dynamically `POST /campaign-emails/bulk`
   - Assigns dynamic credentials extracted safely via Network Requests.
   - Enqueues jobs natively into internal Celery workers

2. **Email Worker** (`app/workers/email_worker.py`)
   - Processes asynchronous pipeline triggers explicitly mapping HTTP structures.
   - Computes templates explicitly bypassing missing local SQL definitions securely.
   - Sends via Gmail API or SMTP implicitly mapped.
   - Decouples native tracker updates effectively firing `execute-reset-sql`.

3. **Email Service** (`app/services/email_service.py`)
   - Fully decoupled logic arrays routing securely back towards SMTP execution.
   - Interacts dynamically mapping secure JSON variables implicitly without SQL objects natively attached.

## Project Structure

```
email-outreach-program/
├── app/
│   ├── core/              # Core configuration and logging wrappers
│   ├── scheduler/         # Application Scheduler API Proxy
│   │   └── campaign_scheduler.py
│   ├── workers/           # Celery execution workers
│   │   ├── celery_app.py
│   │   └── email_worker.py
│   ├── services/          # Abstracted SMTP execution
│   │   └── email_service.py
│   └── integrations/      # External service clients
│       ├── gmail_client.py
│       └── smtp_client.py
├── run_scheduler.py       # Scheduler entry point
├── requirements.txt       # Python dependencies
├── .env.example          # Environment variables template
└── README.md             # This file
```

## Setup

### Prerequisites

- Python 3.11+
- Active FAPI Master Backend running
- Redis 6+

### Installation

1. Clone the repository and install dependencies:

```bash
cd email-outreach-program
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

2. Copy environment variables:

```bash
cp .env.example .env
```

3. Update `.env` with your configuration mapping to external architectures natively:

```env
MAIN_API_BASE_URL=http://localhost:8000
API_BEARER_TOKEN=put_your_secure_secret_token_here
REDIS_URL=redis://localhost:6379/0
GOOGLE_CLIENT_ID=your-client-id
GOOGLE_CLIENT_SECRET=your-client-secret
SECRET_KEY=your-secret-key
```

## Running the Service

### 1. Start Redis

```bash
redis-server
# or natively via docker: docker-compose up -d redis
```

### 2. Start Celery Workers

```bash
celery -A app.workers.celery_app worker --loglevel=info --concurrency=4
```

For production with monitoring:

```bash
celery -A app.workers.celery_app worker \
    --loglevel=info \
    --concurrency=10 \
    --max-tasks-per-child=1000 \
    --time-limit=300
```

### 3. Start the Scheduler

**Run once:**

```bash
python run_scheduler.py --once
```

**Run continuously (recommended for production):**

```bash
python run_scheduler.py
```

**Custom interval:**

```bash
python run_scheduler.py --interval 30  # Run every 30 seconds
```

**Production with systemd:**

Create `/etc/systemd/system/email-scheduler.service`:

```ini
[Unit]
Description=Email Outreach Scheduler
After=network.target redis.service

[Service]
Type=simple
WorkingDirectory=/path/to/email-outreach-program
Environment="PATH=/path/to/venv/bin"
ExecStart=/path/to/venv/bin/python run_scheduler.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl enable email-scheduler
sudo systemctl start email-scheduler
sudo systemctl status email-scheduler
```

## How It Works

### Campaign Execution Flow

1. **Scheduler Selection**
   - Every minute, the scheduler queries `GET /orchestrator/schedules/due` over the network.
   - Discards legacy models and natively retrieves completely structured FAPI JSON representations securely.
   - Pushes target objects cleanly via mass tracking into the `POST /campaign-emails/bulk` FAPI endpoint.

2. **Job Enqueueing**
   - Combines parameters implicitly parsed from backend triggers.
   - Generates Celery queue components natively mapping dynamic variables into parallel sequences randomly delayed (30-120 seconds default).

3. **Worker Processing**
   - Picks up target JSON templates asynchronously natively.
   - Generates secure SMTP/Gmail hooks.
   - Connects back aggressively against FAPI via Webhooks specifically passing `recipient_update_sql` parameters dynamically handling deduplications inherently server-side securely.
   - Explicitly creates master `automation_workflow_logs` arrays synchronously capturing execution logs.

### Template Variables

Available variables dynamically bound explicitly from the JSON `execute-recipient-sql` return arrays:

```jinja2
Hi {{first_name}},

I noticed {{company}} is hiring for a {{title}} position.

Best regards,
{{campaign_name}}
```

## Configuration

### Scheduler Settings

```python
SCHEDULER_INTERVAL_SECONDS=60  # How often to run
SCHEDULER_BATCH_SIZE=100       # Max leads per run
```

### Rate Limiting

```python
DEFAULT_DAILY_LIMIT=50         # Default per account
MIN_DELAY_SECONDS=30          # Min delay between emails
MAX_DELAY_SECONDS=120         # Max delay between emails
```

## Monitoring

### Logs

The service logs explicitly using structured JSON tracking execution loops dynamically reporting implicitly to the FAPI server endpoints asynchronously:

```python
logger.info(
    "email_sent_successfully",
    campaign_lead_id=123,
    to_email="user@example.com",
    from_email="sender@yourdomain.com"
)
```

### Celery Monitoring

Use Flower for web-based monitoring inherently tracking queue backpressure logic:

```bash
pip install flower
celery -A app.workers.celery_app flower
```

Access at `http://localhost:5555`

### Health Checks

Check scheduler status:

```bash
systemctl status email-scheduler
```

Check Celery workers:

```bash
celery -A app.workers.celery_app inspect active
celery -A app.workers.celery_app inspect stats
```

## Production Considerations

### Security

- Encrypt `gmail_refresh_token` and `smtp_password` securely at rest FAPI-side natively.
- API endpoints act specifically as the network boundary preventing unauthorized SQL interactions exclusively.

### Scalability

- Because there are zero generic PostgreSQL bounds inherently restricting operations, you can strictly infinitely replicate worker nodes.

### Performance

- Ensure your external FAPI HTTP network handles `POST /campaign-emails/bulk` asynchronously implicitly.

## Troubleshooting

### No emails being sent

1. Check scheduler is running:

   ```bash
   ps aux | grep run_scheduler
   ```

2. Check Celery workers:

   ```bash
   celery -A app.workers.celery_app inspect active
   ```

3. Ensure internal `MAIN_API_BASE_URL` securely aligns directly properly on your FAPI server explicitly configured natively! Ensure endpoints cleanly expose `fapi` components accurately explicitly.

### Gmail API errors

- Verify refresh token is valid
- Check OAuth scopes include `gmail.send`
- Ensure Google Cloud project has Gmail API enabled
- Check for rate limit errors in logs

### SMTP errors

- Verify SMTP credentials dynamically fetched over REST.
- Check firewall allows outbound connections
- Ensure TLS settings match server requirements
- Review SMTP server rate limits

## License

Proprietary - All rights reserved
