"""
Outreach Run Report Service

Sends a beautiful rich HTML email report to the admin after
each daily outreach dispatch completes.
"""

import smtplib
import uuid
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


def _build_html_report(
    candidate_name: str,
    candidate_id: int,
    run_date: str,
    total_dispatched: int,
    success_count: int,
    failed_count: int,
    bounce_count: int,
    pending_remaining: int,
    smtp_account: str,
) -> str:
    """Render the rich HTML email report."""

    success_rate = (
        round((success_count / total_dispatched) * 100)
        if total_dispatched > 0 else 0
    )

    progress_pct = min(
        round(
            (success_count / (success_count + pending_remaining)) * 100
        ) if (success_count + pending_remaining) > 0 else 0,
        100
    )

    status_color = "#22c55e" if success_rate >= 90 else (
        "#f59e0b" if success_rate >= 70 else "#ef4444"
    )
    status_label = (
        "Excellent" if success_rate >= 90
        else ("Good" if success_rate >= 70 else "Needs Attention")
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Outreach Run Report</title>
</head>
<body style="margin:0;padding:0;background:#0f172a;font-family:'Segoe UI',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"
  style="background:#0f172a;min-height:100vh;padding:40px 0;">
  <tr><td align="center">
    <table width="620" cellpadding="0" cellspacing="0"
      style="max-width:620px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:linear-gradient(135deg,#6366f1 0%,#8b5cf6 50%,#a855f7 100%);
        border-radius:16px 16px 0 0;padding:40px 40px 32px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td>
              <div style="font-size:11px;font-weight:700;letter-spacing:3px;
                color:rgba(255,255,255,0.6);text-transform:uppercase;
                margin-bottom:8px;">WEEKLY OUTREACH SYSTEM</div>
              <div style="font-size:28px;font-weight:800;color:#ffffff;
                line-height:1.2;">Daily Run Report</div>
              <div style="font-size:14px;color:rgba(255,255,255,0.75);
                margin-top:6px;">{run_date}</div>
            </td>
            <td align="right" valign="top">
              <div style="background:rgba(255,255,255,0.15);border-radius:12px;
                padding:12px 18px;text-align:center;backdrop-filter:blur(10px);">
                <div style="font-size:11px;color:rgba(255,255,255,0.7);
                  font-weight:600;letter-spacing:1px;">STATUS</div>
                <div style="font-size:15px;font-weight:800;color:#ffffff;
                  margin-top:2px;">{status_label}</div>
                <div style="width:8px;height:8px;border-radius:50%;
                  background:{status_color};margin:6px auto 0;
                  box-shadow:0 0 8px {status_color};"></div>
              </div>
            </td>
          </tr>
        </table>
      </td></tr>

      <!-- CANDIDATE BANNER -->
      <tr><td style="background:#1e293b;padding:20px 40px;border-left:4px solid #6366f1;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td>
              <div style="font-size:11px;color:#94a3b8;font-weight:600;
                letter-spacing:1px;text-transform:uppercase;">Candidate</div>
              <div style="font-size:20px;font-weight:700;color:#f1f5f9;
                margin-top:4px;">{candidate_name}</div>
              <div style="font-size:12px;color:#64748b;margin-top:2px;">
                ID #{candidate_id} &nbsp;·&nbsp; {smtp_account}
              </div>
            </td>
            <td align="right">
              <div style="font-size:12px;color:#94a3b8;">Sent via</div>
              <div style="font-size:13px;font-weight:600;color:#a5b4fc;">
                {smtp_account}
              </div>
            </td>
          </tr>
        </table>
      </td></tr>

      <!-- STAT CARDS -->
      <tr><td style="background:#1e293b;padding:8px 40px 28px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <!-- Total Dispatched -->
            <td width="23%" style="padding:6px;">
              <div style="background:#0f172a;border-radius:12px;padding:20px 14px;
                text-align:center;border:1px solid #334155;">
                <div style="font-size:30px;font-weight:800;color:#6366f1;">
                  {total_dispatched}
                </div>
                <div style="font-size:10px;font-weight:700;letter-spacing:1px;
                  color:#64748b;text-transform:uppercase;margin-top:4px;">
                  Dispatched
                </div>
              </div>
            </td>
            <!-- Success -->
            <td width="23%" style="padding:6px;">
              <div style="background:#0f172a;border-radius:12px;padding:20px 14px;
                text-align:center;border:1px solid #166534;">
                <div style="font-size:30px;font-weight:800;color:#22c55e;">
                  {success_count}
                </div>
                <div style="font-size:10px;font-weight:700;letter-spacing:1px;
                  color:#64748b;text-transform:uppercase;margin-top:4px;">
                  Sent ✓
                </div>
              </div>
            </td>
            <!-- Failed -->
            <td width="23%" style="padding:6px;">
              <div style="background:#0f172a;border-radius:12px;padding:20px 14px;
                text-align:center;border:1px solid #7f1d1d;">
                <div style="font-size:30px;font-weight:800;color:#ef4444;">
                  {failed_count}
                </div>
                <div style="font-size:10px;font-weight:700;letter-spacing:1px;
                  color:#64748b;text-transform:uppercase;margin-top:4px;">
                  Failed ✗
                </div>
              </div>
            </td>
            <!-- Bounced -->
            <td width="23%" style="padding:6px;">
              <div style="background:#0f172a;border-radius:12px;padding:20px 14px;
                text-align:center;border:1px solid #78350f;">
                <div style="font-size:30px;font-weight:800;color:#f59e0b;">
                  {bounce_count}
                </div>
                <div style="font-size:10px;font-weight:700;letter-spacing:1px;
                  color:#64748b;text-transform:uppercase;margin-top:4px;">
                  Bounced
                </div>
              </div>
            </td>
          </tr>
        </table>
      </td></tr>

      <!-- SUCCESS RATE BAR -->
      <tr><td style="background:#1e293b;padding:0 40px 28px;">
        <div style="background:#0f172a;border-radius:12px;padding:20px 24px;
          border:1px solid #334155;">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td>
                <div style="font-size:12px;font-weight:700;color:#94a3b8;
                  letter-spacing:1px;text-transform:uppercase;">
                  Success Rate
                </div>
              </td>
              <td align="right">
                <div style="font-size:20px;font-weight:800;color:{status_color};">
                  {success_rate}%
                </div>
              </td>
            </tr>
          </table>
          <div style="background:#334155;border-radius:999px;height:8px;
            margin-top:12px;overflow:hidden;">
            <div style="background:linear-gradient(90deg,#6366f1,{status_color});
              height:8px;border-radius:999px;width:{success_rate}%;
              transition:width 0.3s ease;">
            </div>
          </div>
        </div>
      </td></tr>

      <!-- CAMPAIGN PROGRESS -->
      <tr><td style="background:#1e293b;padding:0 40px 28px;">
        <div style="background:#0f172a;border-radius:12px;padding:20px 24px;
          border:1px solid #334155;">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td>
                <div style="font-size:12px;font-weight:700;color:#94a3b8;
                  letter-spacing:1px;text-transform:uppercase;">
                  Overall Campaign Progress
                </div>
                <div style="font-size:12px;color:#64748b;margin-top:4px;">
                  {pending_remaining:,} emails still pending in queue
                </div>
              </td>
              <td align="right">
                <div style="font-size:20px;font-weight:800;color:#a5b4fc;">
                  {progress_pct}%
                </div>
              </td>
            </tr>
          </table>
          <div style="background:#334155;border-radius:999px;height:8px;
            margin-top:12px;overflow:hidden;">
            <div style="background:linear-gradient(90deg,#6366f1,#a855f7);
              height:8px;border-radius:999px;width:{progress_pct}%;">
            </div>
          </div>
        </div>
      </td></tr>

      <!-- FOOTER -->
      <tr><td style="background:#1e293b;border-radius:0 0 16px 16px;
        padding:24px 40px;border-top:1px solid #334155;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td>
              <div style="font-size:11px;color:#475569;">
                This is an automated report from your Outreach Service.
                <br>Do not reply to this email.
              </div>
            </td>
            <td align="right">
              <div style="font-size:11px;color:#475569;">
                {run_date}
              </div>
            </td>
          </tr>
        </table>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


def send_run_report(
    candidate_name: str,
    candidate_id: int,
    total_dispatched: int,
    success_count: int,
    failed_count: int,
    bounce_count: int,
    pending_remaining: int,
    smtp_account: str,
) -> None:
    """
    Send the rich HTML daily run report to the configured admin email.
    Silently logs and returns if reporting is not configured.
    """
    if not all([
        settings.report_from_email,
        settings.report_from_password,
        settings.report_recipient_email,
    ]):
        logger.warning(
            "report_skipped_not_configured",
            reason="REPORT_FROM_EMAIL / REPORT_FROM_PASSWORD "
                   "/ REPORT_RECIPIENT_EMAIL not set in .env"
        )
        return

    run_date = datetime.now().strftime("%A, %B %d %Y – %I:%M %p")

    html_body = _build_html_report(
        candidate_name=candidate_name,
        candidate_id=candidate_id,
        run_date=run_date,
        total_dispatched=total_dispatched,
        success_count=success_count,
        failed_count=failed_count,
        bounce_count=bounce_count,
        pending_remaining=pending_remaining,
        smtp_account=smtp_account,
    )

    subject = (
        f"Weekly Outreach Day-wise Report — {candidate_name} · {run_date}"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.report_from_email
    msg["To"] = settings.report_recipient_email
    msg["Message-ID"] = f"<{uuid.uuid4()}@outreach-report>"

    # Plain-text fallback (required for spam filter compatibility)
    plain_body = (
        f"Outreach Run Report\n"
        f"Candidate: {candidate_name} (ID #{candidate_id})\n"
        f"Date: {run_date}\n\n"
        f"Dispatched : {total_dispatched}\n"
        f"Sent       : {success_count}\n"
        f"Failed     : {failed_count}\n"
        f"Bounced    : {bounce_count}\n"
        f"Pending    : {pending_remaining}\n\n"
        f"Sent via: {smtp_account}\n"
    )
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(
            settings.report_smtp_host,
            settings.report_smtp_port
        ) as server:
            server.ehlo()
            server.starttls()
            server.login(
                settings.report_from_email,
                settings.report_from_password
            )
            server.sendmail(
                settings.report_from_email,
                settings.report_recipient_email,
                msg.as_string()
            )

        logger.info(
            "run_report_sent",
            candidate=candidate_name,
            recipient=settings.report_recipient_email,
            dispatched=total_dispatched,
            success=success_count,
        )

    except Exception as e:
        logger.error(
            "run_report_failed",
            error=str(e),
            candidate=candidate_name,
        )


# ── Daily Summary Report ─────────────────────────────────────────────────────

def _build_daily_html_report(
    report_date: str,
    total_sent: int,
    total_failed: int,
    total_bounced: int,
    total_hard: int,
    total_soft: int,
    total_invalid: int,
    candidates: list,          # list of dicts per candidate
) -> str:
    """
    Build a rich HTML daily summary email covering all candidates.

    candidates = [
        {
          "name": str, "id": int,
          "sent": int, "failed": int, "bounced": int,
          "hard": int, "soft": int, "invalid": int,
        },
        ...
    ]
    """
    total_dispatched = total_sent + total_failed + total_bounced
    delivery_rate = (
        round((total_sent / total_dispatched) * 100)
        if total_dispatched > 0 else 0
    )
    status_color = (
        "#22c55e" if delivery_rate >= 90
        else ("#f59e0b" if delivery_rate >= 70 else "#ef4444")
    )
    status_label = (
        "Excellent" if delivery_rate >= 90
        else ("Good" if delivery_rate >= 70 else "Needs Attention")
    )

    # Build per-candidate rows
    candidate_rows = ""
    for c in candidates:
        c_total = c["sent"] + c["failed"] + c["bounced"]
        c_rate = round((c["sent"] / c_total) * 100) if c_total > 0 else 0
        rate_color = (
            "#22c55e" if c_rate >= 90
            else ("#f59e0b" if c_rate >= 70 else "#ef4444")
        )
        candidate_rows += f"""
        <tr>
          <td style="padding:10px 12px;color:#f1f5f9;font-size:13px;
            border-bottom:1px solid #1e293b;">{c['name']}</td>
          <td style="padding:10px 12px;color:#6366f1;font-weight:700;
            text-align:center;font-size:13px;
            border-bottom:1px solid #1e293b;">{c['sent']}</td>
          <td style="padding:10px 12px;color:#ef4444;font-weight:700;
            text-align:center;font-size:13px;
            border-bottom:1px solid #1e293b;">{c['failed']}</td>
          <td style="padding:10px 12px;color:#f59e0b;font-weight:700;
            text-align:center;font-size:13px;
            border-bottom:1px solid #1e293b;">{c['bounced']}</td>
          <td style="padding:10px 12px;text-align:center;
            border-bottom:1px solid #1e293b;">
            <span style="background:{rate_color}22;color:{rate_color};
              font-size:11px;font-weight:700;padding:3px 8px;
              border-radius:999px;">{c_rate}%</span>
          </td>
        </tr>"""

    bounce_breakdown = ""
    if total_bounced > 0:
        bounce_breakdown = f"""
      <!-- BOUNCE BREAKDOWN -->
      <tr><td style="background:#1e293b;padding:0 40px 28px;">
        <div style="background:#0f172a;border-radius:12px;padding:20px 24px;
          border:1px solid #334155;">
          <div style="font-size:12px;font-weight:700;color:#94a3b8;
            letter-spacing:1px;text-transform:uppercase;
            margin-bottom:16px;">Bounce Breakdown</div>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:6px;">
                <div style="background:#0f172a;border-radius:10px;
                  padding:14px;text-align:center;border:1px solid #7f1d1d;">
                  <div style="font-size:22px;font-weight:800;
                    color:#ef4444;">{total_hard}</div>
                  <div style="font-size:10px;color:#64748b;
                    text-transform:uppercase;letter-spacing:1px;
                    margin-top:4px;">Hard</div>
                </div>
              </td>
              <td style="padding:6px;">
                <div style="background:#0f172a;border-radius:10px;
                  padding:14px;text-align:center;border:1px solid #78350f;">
                  <div style="font-size:22px;font-weight:800;
                    color:#f59e0b;">{total_soft}</div>
                  <div style="font-size:10px;color:#64748b;
                    text-transform:uppercase;letter-spacing:1px;
                    margin-top:4px;">Soft</div>
                </div>
              </td>
              <td style="padding:6px;">
                <div style="background:#0f172a;border-radius:10px;
                  padding:14px;text-align:center;border:1px solid #1e3a5f;">
                  <div style="font-size:22px;font-weight:800;
                    color:#60a5fa;">{total_invalid}</div>
                  <div style="font-size:10px;color:#64748b;
                    text-transform:uppercase;letter-spacing:1px;
                    margin-top:4px;">Invalid</div>
                </div>
              </td>
            </tr>
          </table>
        </div>
      </td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Daily Outreach Report</title>
</head>
<body style="margin:0;padding:0;background:#0f172a;
  font-family:'Segoe UI',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"
  style="background:#0f172a;min-height:100vh;padding:40px 0;">
  <tr><td align="center">
    <table width="640" cellpadding="0" cellspacing="0"
      style="max-width:640px;width:100%;">

      <!-- HEADER -->
      <tr><td style="background:linear-gradient(135deg,
        #6366f1 0%,#8b5cf6 50%,#a855f7 100%);
        border-radius:16px 16px 0 0;padding:40px 40px 32px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td>
              <div style="font-size:11px;font-weight:700;letter-spacing:3px;
                color:rgba(255,255,255,0.6);text-transform:uppercase;
                margin-bottom:8px;">WEEKLY OUTREACH SYSTEM</div>
              <div style="font-size:28px;font-weight:800;color:#ffffff;
                line-height:1.2;">Daily Run Report</div>
              <div style="font-size:14px;color:rgba(255,255,255,0.75);
                margin-top:6px;">{report_date}</div>
            </td>
            <td align="right" valign="top">
              <div style="background:rgba(255,255,255,0.15);
                border-radius:12px;padding:12px 18px;text-align:center;">
                <div style="font-size:11px;color:rgba(255,255,255,0.7);
                  font-weight:600;letter-spacing:1px;">STATUS</div>
                <div style="font-size:15px;font-weight:800;color:#ffffff;
                  margin-top:2px;">{status_label}</div>
                <div style="width:8px;height:8px;border-radius:50%;
                  background:{status_color};margin:6px auto 0;
                  box-shadow:0 0 8px {status_color};"></div>
              </div>
            </td>
          </tr>
        </table>
      </td></tr>

      <!-- TOTALS -->
      <tr><td style="background:#1e293b;padding:24px 40px;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td width="25%" style="padding:6px;">
              <div style="background:#0f172a;border-radius:12px;
                padding:20px 14px;text-align:center;
                border:1px solid #334155;">
                <div style="font-size:32px;font-weight:800;
                  color:#6366f1;">{total_dispatched}</div>
                <div style="font-size:10px;font-weight:700;
                  letter-spacing:1px;color:#64748b;
                  text-transform:uppercase;margin-top:4px;">Dispatched</div>
              </div>
            </td>
            <td width="25%" style="padding:6px;">
              <div style="background:#0f172a;border-radius:12px;
                padding:20px 14px;text-align:center;
                border:1px solid #166534;">
                <div style="font-size:32px;font-weight:800;
                  color:#22c55e;">{total_sent}</div>
                <div style="font-size:10px;font-weight:700;
                  letter-spacing:1px;color:#64748b;
                  text-transform:uppercase;margin-top:4px;">Sent ✓</div>
              </div>
            </td>
            <td width="25%" style="padding:6px;">
              <div style="background:#0f172a;border-radius:12px;
                padding:20px 14px;text-align:center;
                border:1px solid #7f1d1d;">
                <div style="font-size:32px;font-weight:800;
                  color:#ef4444;">{total_failed}</div>
                <div style="font-size:10px;font-weight:700;
                  letter-spacing:1px;color:#64748b;
                  text-transform:uppercase;margin-top:4px;">Failed ✗</div>
              </div>
            </td>
            <td width="25%" style="padding:6px;">
              <div style="background:#0f172a;border-radius:12px;
                padding:20px 14px;text-align:center;
                border:1px solid #78350f;">
                <div style="font-size:32px;font-weight:800;
                  color:#f59e0b;">{total_bounced}</div>
                <div style="font-size:10px;font-weight:700;
                  letter-spacing:1px;color:#64748b;
                  text-transform:uppercase;margin-top:4px;">Bounced</div>
              </div>
            </td>
          </tr>
        </table>
      </td></tr>

      <!-- DELIVERY RATE BAR -->
      <tr><td style="background:#1e293b;padding:0 40px 24px;">
        <div style="background:#0f172a;border-radius:12px;
          padding:20px 24px;border:1px solid #334155;">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td>
                <div style="font-size:12px;font-weight:700;color:#94a3b8;
                  letter-spacing:1px;text-transform:uppercase;">
                  Overall Delivery Rate</div>
              </td>
              <td align="right">
                <div style="font-size:22px;font-weight:800;
                  color:{status_color};">{delivery_rate}%</div>
              </td>
            </tr>
          </table>
          <div style="background:#334155;border-radius:999px;height:8px;
            margin-top:12px;overflow:hidden;">
            <div style="background:linear-gradient(90deg,
              #6366f1,{status_color});height:8px;border-radius:999px;
              width:{delivery_rate}%;"></div>
          </div>
        </div>
      </td></tr>

      {bounce_breakdown}

      <!-- PER CANDIDATE TABLE -->
      <tr><td style="background:#1e293b;padding:0 40px 28px;">
        <div style="background:#0f172a;border-radius:12px;
          border:1px solid #334155;overflow:hidden;">
          <div style="padding:16px 20px;border-bottom:1px solid #1e293b;">
            <div style="font-size:12px;font-weight:700;color:#94a3b8;
              letter-spacing:1px;text-transform:uppercase;">
              Per Candidate Breakdown</div>
          </div>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr style="background:#0d1526;">
              <th style="padding:10px 12px;text-align:left;font-size:11px;
                color:#64748b;font-weight:700;letter-spacing:1px;
                text-transform:uppercase;">Candidate</th>
              <th style="padding:10px 12px;text-align:center;font-size:11px;
                color:#64748b;font-weight:700;letter-spacing:1px;
                text-transform:uppercase;">Sent</th>
              <th style="padding:10px 12px;text-align:center;font-size:11px;
                color:#64748b;font-weight:700;letter-spacing:1px;
                text-transform:uppercase;">Failed</th>
              <th style="padding:10px 12px;text-align:center;font-size:11px;
                color:#64748b;font-weight:700;letter-spacing:1px;
                text-transform:uppercase;">Bounced</th>
              <th style="padding:10px 12px;text-align:center;font-size:11px;
                color:#64748b;font-weight:700;letter-spacing:1px;
                text-transform:uppercase;">Rate</th>
            </tr>
            {candidate_rows}
          </table>
        </div>
      </td></tr>

      <!-- FOOTER -->
      <tr><td style="background:#1e293b;border-radius:0 0 16px 16px;
        padding:24px 40px;border-top:1px solid #334155;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td>
              <div style="font-size:11px;color:#475569;">
                Automated daily report · Outreach Service<br>
                Do not reply to this email.
              </div>
            </td>
            <td align="right">
              <div style="font-size:11px;color:#475569;">{report_date}</div>
            </td>
          </tr>
        </table>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


def send_daily_report(candidates: list) -> None:
    """
    Send a daily summary report covering ALL candidates that ran today.

    Args:
        candidates: List of dicts, one per candidate:
            {
              "name": str, "id": int,
              "sent": int, "failed": int, "bounced": int,
              "hard": int, "soft": int, "invalid": int,
            }
    """
    if not all([
        settings.report_from_email,
        settings.report_from_password,
        settings.report_recipient_email,
    ]):
        logger.warning(
            "daily_report_skipped_not_configured",
            reason="REPORT_* env vars not set",
        )
        return

    if not candidates:
        logger.info("daily_report_skipped_no_data")
        return

    # Aggregate totals
    total_sent    = sum(c["sent"]    for c in candidates)
    total_failed  = sum(c["failed"]  for c in candidates)
    total_bounced = sum(c["bounced"] for c in candidates)
    total_hard    = sum(c.get("hard",    0) for c in candidates)
    total_soft    = sum(c.get("soft",    0) for c in candidates)
    total_invalid = sum(c.get("invalid", 0) for c in candidates)

    report_date = datetime.now().strftime("%A, %B %d %Y")
    total = total_sent + total_failed + total_bounced

    html_body = _build_daily_html_report(
        report_date=report_date,
        total_sent=total_sent,
        total_failed=total_failed,
        total_bounced=total_bounced,
        total_hard=total_hard,
        total_soft=total_soft,
        total_invalid=total_invalid,
        candidates=candidates,
    )

    subject = (
        f"Weekly Outreach Day-wise Report — {report_date}"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = settings.report_from_email
    msg["To"]      = settings.report_recipient_email
    msg["Message-ID"] = f"<{uuid.uuid4()}@outreach-daily>"

    plain = (
        f"Daily Outreach Report — {report_date}\n\n"
        f"Total Dispatched : {total}\n"
        f"Sent             : {total_sent}\n"
        f"Failed           : {total_failed}\n"
        f"Bounced          : {total_bounced}\n"
        f"  Hard           : {total_hard}\n"
        f"  Soft           : {total_soft}\n"
        f"  Invalid        : {total_invalid}\n\n"
        "Per Candidate:\n"
    )
    for c in candidates:
        c_total = c["sent"] + c["failed"] + c["bounced"]
        plain += (
            f"  {c['name']:30s} "
            f"Sent={c['sent']}  Failed={c['failed']}  "
            f"Bounced={c['bounced']}  "
            f"Total={c_total}\n"
        )

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(
            settings.report_smtp_host,
            settings.report_smtp_port,
        ) as server:
            server.ehlo()
            server.starttls()
            server.login(
                settings.report_from_email,
                settings.report_from_password,
            )
            server.sendmail(
                settings.report_from_email,
                settings.report_recipient_email,
                msg.as_string(),
            )

        logger.info(
            "daily_report_sent",
            recipient=settings.report_recipient_email,
            candidates=len(candidates),
            sent=total_sent,
            total=total,
        )

    except Exception as e:
        logger.error("daily_report_failed", error=str(e))

