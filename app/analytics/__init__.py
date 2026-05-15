"""
Analytics module — DuckDB-based export and analysis of campaign email data.

Exports completed campaign_emails records from the Orchestrator API
into Parquet files, then queries them via DuckDB for bounce analysis,
delivery reporting, and suppression list generation.
"""
