"""pbi-watchdog — watchdog de capacidade para Power BI / Microsoft Fabric.

Uso mínimo::

    from pbi_watchdog import WatchdogConfig, Watchdog

    config = WatchdogConfig.from_file("watchdog.yaml")
    for summary in Watchdog(config).run_once():
        print(summary.capacity_key, summary.anomalies, summary.actions_taken)
"""

from .config import (
    CapacityConfig,
    PolicyConfig,
    ServicePrincipalAuth,
    WatchdogConfig,
)
from .models import Assessment, Event, Interval, ItemSnapshot, RunSummary, Tier
from .runner import Watchdog

__version__ = "0.1.0"

__all__ = [
    "Assessment",
    "CapacityConfig",
    "Event",
    "Interval",
    "ItemSnapshot",
    "PolicyConfig",
    "RunSummary",
    "ServicePrincipalAuth",
    "Tier",
    "Watchdog",
    "WatchdogConfig",
    "__version__",
]
