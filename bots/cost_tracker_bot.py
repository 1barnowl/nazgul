#!/usr/bin/env python3
"""
cost_tracker_bot.py — Cost Tracker Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Queries AWS, Azure, and GCP billing APIs to report
current month's spend and project end-of-month cost.
Alerts if projected cost exceeds configured budget.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests boto3 azure-identity azure-mgmt-costmanagement google-cloud-billing-budgets

Configuration
─────────────
Place `cost_tracker_config.json` in the same directory:

{
  "aws": {
    "enabled": true,
    "region": "us-east-1",
    "budget_threshold": 500.0
  },
  "azure": {
    "enabled": true,
    "subscription_id": "00000000-0000-0000-0000-000000000000",
    "budget_threshold": 500.0
  },
  "gcp": {
    "enabled": true,
    "billing_account_id": "AAAAAA-XXXXXX-YYYYYY",
    "budget_ids": ["my-budget-id"],
    "budget_threshold": 500.0
  },
  "scan_interval_minutes": 360
}
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "cost_tracker_bot"
BOT_NAME = "Cost Tracker"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

# ── Configuration path ────────────────────────────────────────────────────────
CONFIG_NAME = "cost_tracker_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ────────────────────────────────────────────────────────────────
def _post(summary: str, level: str = "info", payload: dict = None) -> None:
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id":   BOT_ID,
            "bot_name": BOT_NAME,
            "summary":  summary,
            "level":    level,
            "payload":  payload or {},
        }, timeout=5)
    except Exception:
        pass

def _heartbeat() -> None:
    global _last_hb
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME,
            "status":   "online",
        }, timeout=3)
    except Exception:
        pass
    _last_hb = time.time()

# ── AWS Cost ──────────────────────────────────────────────────────────────────
def get_aws_cost(region: str) -> dict:
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        return {"error": "boto3 not installed"}
    try:
        ce = boto3.client("ce", region_name=region)
        now = datetime.now(timezone.utc)
        start = now.replace(day=1).strftime("%Y-%m-%d")
        end = now.strftime("%Y-%m-%d")

        # Actual cost to date
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"]
        )
        total = 0.0
        for r in resp["ResultsByTime"]:
            total += float(r["Total"]["UnblendedCost"]["Amount"])

        # Forecast to month end
        forecast = total
        try:
            forecast_resp = ce.get_cost_forecast(
                TimePeriod={"Start": start, "End": now.strftime("%Y-%m-%d")},
                Metric="UNBLENDED_COST",
                Granularity="MONTHLY"
            )
            forecast = float(forecast_resp["Total"]["Amount"])
        except (ClientError, KeyError):
            # Fallback: daily rate projection
            days_passed = max(1, (now - now.replace(day=1)).days)
            days_in_month = (now.replace(month=now.month % 12 + 1, day=1) - timedelta(days=1)).day
            if days_passed > 0:
                forecast = total / days_passed * days_in_month

        return {"current_spend": round(total, 2), "projected_spend": round(forecast, 2)}
    except Exception as e:
        return {"error": str(e)[:200]}

# ── Azure Cost ────────────────────────────────────────────────────────────────
def get_azure_cost(subscription_id: str) -> dict:
    try:
        from azure.identity import DefaultAzureCredential
        from azure.mgmt.costmanagement import CostManagementClient
        from azure.mgmt.costmanagement.models import (
            QueryDefinition, QueryDataset, QueryTimePeriod, QueryAggregation,
            ExportType, TimeframeType, GranularityType
        )
    except ImportError:
        return {"error": "azure-identity and/or azure-mgmt-costmanagement not installed"}
    try:
        credential = DefaultAzureCredential()
        client = CostManagementClient(credential)
        scope = f"subscriptions/{subscription_id}"
        now = datetime.now(timezone.utc)
        start = now.replace(day=1)
        end = now

        time_period = QueryTimePeriod(
            from_property=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            to=end.strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        query = QueryDefinition(
            type=ExportType.ACTUAL_COST,
            timeframe=TimeframeType.CUSTOM,
            time_period=time_period,
            dataset=QueryDataset(
                granularity=GranularityType.DAILY,
                aggregation={
                    "totalCost": QueryAggregation(name="Cost", function="Sum")
                }
            )
        )
        result = client.query.usage(scope=scope, parameters=query)
        total = 0.0
        if result.rows:
            for row in result.rows:
                # row[0] is cost for that day, row indices depend on schema.
                # Typically column 0 is the aggregation value.
                total += float(row[0]) if len(row) > 0 else 0.0

        # Project to month end
        days_passed = max(1, (now - start).days)
        days_in_month = (now.replace(month=now.month % 12 + 1, day=1) - timedelta(days=1)).day
        projected = total / days_passed * days_in_month if days_passed > 0 else total

        return {"current_spend": round(total, 2), "projected_spend": round(projected, 2)}
    except Exception as e:
        return {"error": str(e)[:200]}

# ── GCP Cost ──────────────────────────────────────────────────────────────────
def get_gcp_cost(billing_account_id: str, budget_ids: list) -> dict:
    try:
        from google.cloud.billing.budgets_v1beta1 import BudgetServiceClient
    except ImportError:
        return {"error": "google-cloud-billing-budgets (v1beta1) not installed"}
    try:
        client = BudgetServiceClient()
        grand_total = 0.0
        details = {}
        for budget_id in budget_ids:
            name = f"billingAccounts/{billing_account_id}/budgets/{budget_id}"
            try:
                budget = client.get_budget(name=name)
                actual = getattr(budget, "actual_spend", None)
                if actual and hasattr(actual, "amount"):
                    units = getattr(actual.amount, "units", 0)
                    nanos = getattr(actual.amount, "nanos", 0)
                    spend = float(units) + nanos / 1e9
                    grand_total += spend
                    details[budget_id] = round(spend, 2)
                else:
                    details[budget_id] = 0.0
            except Exception as e:
                details[budget_id] = {"error": str(e)[:200]}
        return {
            "current_spend": round(grand_total, 2),
            "budgets": details,
            "billing_account": billing_account_id
        }
    except Exception as e:
        return {"error": f"GCP client error: {str(e)[:200]}"}

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _post("Cost Tracker Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        scan_seconds = int(config.get("scan_interval_minutes", 360)) * 60

        # AWS
        aws_cfg = config.get("aws", {})
        if aws_cfg.get("enabled"):
            aws_result = get_aws_cost(aws_cfg.get("region", "us-east-1"))
            if "current_spend" in aws_result:
                threshold = float(aws_cfg.get("budget_threshold", 500))
                level = "error" if aws_result["projected_spend"] > threshold else "info"
                _post(
                    f"AWS cost: ${aws_result['current_spend']:.2f} so far, projected ${aws_result['projected_spend']:.2f} "
                    f"(budget ${threshold:.0f})",
                    level,
                    {"provider": "aws", "data": aws_result, "budget_threshold": threshold}
                )
            else:
                _post(f"AWS cost check failed: {aws_result.get('error', 'unknown')}", "warning")

        # Azure
        azure_cfg = config.get("azure", {})
        if azure_cfg.get("enabled"):
            azure_result = get_azure_cost(azure_cfg["subscription_id"])
            if "current_spend" in azure_result:
                threshold = float(azure_cfg.get("budget_threshold", 500))
                level = "error" if azure_result["projected_spend"] > threshold else "info"
                _post(
                    f"Azure cost: ${azure_result['current_spend']:.2f} so far, projected ${azure_result['projected_spend']:.2f} "
                    f"(budget ${threshold:.0f})",
                    level,
                    {"provider": "azure", "data": azure_result, "budget_threshold": threshold}
                )
            else:
                _post(f"Azure cost check failed: {azure_result.get('error', 'unknown')}", "warning")

        # GCP
        gcp_cfg = config.get("gcp", {})
        if gcp_cfg.get("enabled"):
            gcp_result = get_gcp_cost(gcp_cfg["billing_account_id"], gcp_cfg.get("budget_ids", []))
            if "current_spend" in gcp_result:
                threshold = float(gcp_cfg.get("budget_threshold", 500))
                level = "error" if gcp_result["current_spend"] > threshold else "info"
                _post(
                    f"GCP cost: ${gcp_result['current_spend']:.2f} across budgets "
                    f"(threshold ${threshold:.0f})",
                    level,
                    {"provider": "gcp", "data": gcp_result, "budget_threshold": threshold}
                )
            else:
                _post(f"GCP cost check failed: {gcp_result.get('error', 'unknown')}", "warning")

        _heartbeat()
        time.sleep(scan_seconds)

if __name__ == "__main__":
    main()
