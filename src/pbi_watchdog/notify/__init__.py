"""Canais de notificação. Falha de canal nunca interrompe o ciclo — apenas loga."""

from __future__ import annotations

import logging
from typing import List, Optional, Protocol, Sequence

import requests

from ..config import NotifierConfig
from ..models import ActionResult, Assessment, Tier

log = logging.getLogger(__name__)

_TIER_ICON = {Tier.ALERT: "⚠️", Tier.THROTTLE: "🛑", Tier.KILL: "🔴"}


def format_message(a: Assessment, mode: str, results: Optional[Sequence[ActionResult]] = None) -> str:
    icon = _TIER_ICON.get(a.tier, "•")
    ratio = f"{a.ratio:.2f}x" if a.ratio else "n/d"
    base = a.baseline.value if a.baseline else 0.0
    lines = [
        f"{icon} **[{a.capacity_key}]** `{a.interval.item_name}` ({a.interval.item_kind}) "
        f"a **{ratio}** da baseline",
        f"Consumo: {a.interval.cu_seconds:,.0f} CU·s em {a.interval.minutes:.0f} min "
        f"| baseline: {base:,.0f} CU·s "
        f"| workspace: {a.interval.workspace_name or a.interval.workspace_id}",
        f"Detectado: `{a.tier.label}` → aplicado: `{a.effective_tier.label}` (modo `{mode}`)",
    ]
    if a.suppressions:
        lines.append(f"Contido por: {', '.join(a.suppressions)}")
    if results:
        done = [
            f"{r.action}={'ok' if r.ok else 'FALHOU'} ({r.detail})"
            for r in results
            if r.action != "notify"
        ]
        if done:
            lines.append("Ações: " + " | ".join(done))
    lines.append(f"Item ID: `{a.item_id}`")
    return "\n".join(lines)


class Notifier(Protocol):
    def send(self, text: str, tier: Tier) -> bool: ...


class _Base:
    def __init__(self, cfg: NotifierConfig) -> None:
        self.cfg = cfg
        self.min_tier = Tier[cfg.min_tier.upper()]

    def wants(self, tier: Tier) -> bool:
        return tier >= self.min_tier


class ConsoleNotifier(_Base):
    def send(self, text: str, tier: Tier) -> bool:
        if not self.wants(tier):
            return True
        print(text)
        return True


class TeamsNotifier(_Base):
    """Incoming webhook clássico ou Workflows (Power Automate). Envia MessageCard, que
    ambos aceitam."""

    def send(self, text: str, tier: Tier) -> bool:
        if not self.wants(tier):
            return True
        color = {Tier.ALERT: "FFC107", Tier.THROTTLE: "FF7043", Tier.KILL: "D32F2F"}.get(tier, "808080")
        payload = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": color,
            "summary": "Power BI Capacity Watchdog",
            "title": f"Capacity Watchdog — {tier.label.upper()}",
            "text": text.replace("\n", "\n\n"),
        }
        return self._post(payload)

    def _post(self, payload: dict) -> bool:
        try:
            r = requests.post(self.cfg.url, json=payload, timeout=self.cfg.timeout_seconds)
            if r.status_code >= 400:
                log.warning("Teams webhook HTTP %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except requests.RequestException as e:
            log.warning("Teams webhook falhou: %s", e)
            return False


class SlackNotifier(_Base):
    def send(self, text: str, tier: Tier) -> bool:
        if not self.wants(tier):
            return True
        try:
            r = requests.post(
                self.cfg.url, json={"text": text}, timeout=self.cfg.timeout_seconds
            )
            return r.status_code < 400
        except requests.RequestException as e:
            log.warning("Slack webhook falhou: %s", e)
            return False


class WebhookNotifier(_Base):
    """POST genérico com o payload estruturado — para integrar com ServiceNow, PagerDuty, etc."""

    def send(self, text: str, tier: Tier) -> bool:
        if not self.wants(tier):
            return True
        try:
            r = requests.post(
                self.cfg.url,
                json={"tier": tier.label, "text": text},
                timeout=self.cfg.timeout_seconds,
            )
            return r.status_code < 400
        except requests.RequestException as e:
            log.warning("Webhook falhou: %s", e)
            return False


class NullNotifier(_Base):
    def send(self, text: str, tier: Tier) -> bool:
        return True


_KINDS = {
    "teams": TeamsNotifier,
    "slack": SlackNotifier,
    "webhook": WebhookNotifier,
    "console": ConsoleNotifier,
    "none": NullNotifier,
}


def build_notifiers(configs: Sequence[NotifierConfig]) -> List[Notifier]:
    return [_KINDS[c.kind](c) for c in configs]


def broadcast(notifiers: Sequence[Notifier], text: str, tier: Tier) -> None:
    for n in notifiers:
        try:
            n.send(text, tier)
        except Exception as e:
            # Canal quebrado não derruba o ciclo — nem impede os outros canais.
            log.warning("Notificador %s falhou: %s", type(n).__name__, e)
