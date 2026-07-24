"""Diagnóstico pré-voo.

Responde à pergunta que todo mundo faz na primeira instalação: *o que exatamente falta
para isto funcionar?* Cada checagem testa uma capacidade concreta e, quando falha, diz
qual permissão/configuração de tenant resolve.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Callable, List, Optional

from . import clock
from .auth import SCOPE_FABRIC, SCOPE_POWERBI, build_token_provider
from .config import ServicePrincipalAuth, WatchdogConfig
from .rest import PBI_BASE, ApiError, RestClient
from .sources import MetricsAppRestSource, build_source
from .storage import build_store

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"


@dataclasses.dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    remedy: str = ""

    @property
    def icon(self) -> str:
        return {OK: "✅", WARN: "⚠️ ", FAIL: "❌", SKIP: "⏭️ "}[self.status]


def _check(name: str, fn: Callable[[], Check], remedy: str = "") -> Check:
    """Uma checagem que explode ainda precisa se apresentar pelo nome de negócio, não pelo
    nome da função: o doctor existe para ser lido por quem está provisionando, não por quem
    escreveu a lib."""
    try:
        return fn()
    except Exception as e:
        # O doctor reporta falhas, nunca propaga: seu trabalho é produzir o relatório completo.
        return Check(name, FAIL, detail=f"{type(e).__name__}: {e}", remedy=remedy)


class Doctor:
    def __init__(self, config: WatchdogConfig) -> None:
        self.config = config
        self.checks: List[Check] = []
        self._client: Optional[RestClient] = None

    #: rótulo e remédio usados quando a checagem levanta antes de produzir seu próprio Check.
    _AUTH_REMEDY = (
        "Verifique tenant_id (GUID ou domínio), client_id e o segredo/certificado. "
        "Confirme também que o SPN está no grupo habilitado em 'Service principals can "
        "use Fabric APIs' no Admin Portal."
    )

    def run(self, *, deep: bool = False) -> List[Check]:
        self.checks = []
        self._add("Config válida", self.check_config)
        self._add("Token Power BI", self.check_auth_powerbi, self._AUTH_REMEDY)
        self._add("Token Fabric", self.check_auth_fabric, self._AUTH_REMEDY)
        self._add("Storage gravável", self.check_storage,
                  "Confira storage.path: o diretório precisa existir e ser gravável.")
        self._add("Acesso ao Metrics App", self.check_metrics_access, self._AUTH_REMEDY)
        self._add("Perfil de DAX", self.check_metrics_profile,
                  "Rode `pbi-watchdog inspect-model -v` e ajuste metrics_source.dax_override.")
        self._add("Capacidades visíveis", self.check_capacities, self._AUTH_REMEDY)
        self._add("Notificação", self.check_notifiers)
        if deep:
            self._add("Alcance de escrita", self.check_admin_scope, self._AUTH_REMEDY)
            self._add("Snapshot real", self.check_snapshot,
                      "Um snapshot que explode indica DAX incompatível ou capacity_id inválido.")
        self._add("Prontidão para enforce", self.check_enforcement_readiness)
        return self.checks

    def _add(self, label: str, fn: Callable[[], Check], remedy: str = "") -> None:
        self.checks.append(_check(label, fn, remedy))

    # ------------------------------------------------------------------ checks

    def client(self) -> RestClient:
        if self._client is None:
            self._client = RestClient(build_token_provider(self.config.auth))
        return self._client

    def check_config(self) -> Check:
        n = len(self.config.enabled_capacities)
        modes = {c.key: (c.policy.mode if c.policy else "?") for c in self.config.enabled_capacities}
        return Check(
            "Config válida", OK, detail=f"{n} capacidade(s) habilitada(s): {modes}"
        )

    def check_auth_powerbi(self) -> Check:
        tok = build_token_provider(self.config.auth).get_token(SCOPE_POWERBI)
        return Check("Token Power BI", OK, detail=f"adquirido ({len(tok)} chars)")

    def check_auth_fabric(self) -> Check:
        try:
            build_token_provider(self.config.auth).get_token(SCOPE_FABRIC)
            return Check("Token Fabric", OK)
        except Exception as e:
            return Check(
                "Token Fabric", WARN, detail=str(e),
                remedy="Sem token Fabric a ação 'cancel_fabric_jobs' fica indisponível; "
                       "alertas e cancel_refresh seguem funcionando.",
            )

    def check_storage(self) -> Check:
        store = build_store(self.config.storage)
        store.init_schema()
        store.close()
        return Check("Storage gravável", OK, detail=self.config.storage.kind)

    def check_metrics_access(self) -> Check:
        cfg = self.config.metrics_source
        if cfg.kind != "metrics_app_rest":
            return Check("Acesso ao Metrics App", SKIP, detail=f"kind={cfg.kind}")
        try:
            rows = self.client().execute_dax(cfg.workspace_id, cfg.dataset_id, "EVALUATE ROW(\"ping\", 1)")
            return Check("Acesso ao Metrics App", OK, detail=f"executeQueries respondeu ({len(rows)} linha)")
        except ApiError as e:
            remedy = (
                "Habilite no Admin Portal: 'Dataset Execute Queries REST API' (Developer settings) "
                "e 'Service principals can use Fabric APIs'. O SPN precisa ser membro (Viewer+) "
                "do workspace do Capacity Metrics App."
            )
            if e.status == 401:
                remedy = "Token rejeitado. Verifique tenant_id/client_id/secret e o consentimento de admin."
            elif e.status == 404:
                remedy = "workspace_id/dataset_id incorretos. Rode `pbi-watchdog discover --metrics`."
            return Check("Acesso ao Metrics App", FAIL, detail=f"HTTP {e.status}", remedy=remedy)

    def check_metrics_profile(self) -> Check:
        cfg = self.config.metrics_source
        if cfg.kind == "fake":
            return Check("Perfil de DAX", SKIP, detail="fonte fake")
        source = build_source(cfg, self.client() if cfg.kind == "metrics_app_rest" else None)
        if not isinstance(source, MetricsAppRestSource):
            return Check("Perfil de DAX", SKIP, detail=f"kind={cfg.kind}")
        try:
            capacities = self.config.enabled_capacities
            if not capacities:
                return Check("Perfil de DAX", FAIL, detail="nenhuma capacidade habilitada")
            profile = source.resolve_profile(capacities[0].id)
            return Check("Perfil de DAX", OK, detail=f"{profile.name} — {profile.description}")
        except RuntimeError as e:
            return Check(
                "Perfil de DAX", FAIL, detail=str(e).splitlines()[0],
                remedy="Rode `pbi-watchdog inspect-model` para ver o modelo e preencha "
                       "metrics_source.dax_override.",
            )

    def check_capacities(self) -> Check:
        """Confirma que os GUIDs configurados existem e que o principal os enxerga."""
        try:
            data = self.client().get(f"{PBI_BASE}/admin/capacities")
        except ApiError as e:
            return Check(
                "Capacidades visíveis", WARN, detail=f"HTTP {e.status} em /admin/capacities",
                remedy="O SPN precisa de Tenant.Read.All (ou Capacity.Read.All) e estar num "
                       "grupo permitido em 'Service principals can use read-only admin APIs'. "
                       "Sem isso, o watchdog ainda funciona, mas não valida os GUIDs.",
            )
        known = {c.get("id", "").lower(): c for c in data.get("value", [])}
        missing, found = [], []
        for cap in self.config.enabled_capacities:
            entry = known.get(cap.id.lower())
            if entry:
                found.append(f"{cap.key}={entry.get('displayName')} ({entry.get('sku')})")
            else:
                missing.append(cap.key)
        if missing:
            return Check(
                "Capacidades visíveis", FAIL,
                detail=f"não encontradas: {missing}",
                remedy="Confira os GUIDs com `pbi-watchdog discover --capacities`.",
            )
        return Check("Capacidades visíveis", OK, detail="; ".join(found))

    def check_admin_scope(self) -> Check:
        """Cancelar refresh/job exige permissão de escrita no workspace, não só admin de leitura."""
        problems = []
        try:
            groups = self.client().get(f"{PBI_BASE}/groups", params={"$top": 1})
            n = len(groups.get("value", []))
        except ApiError as e:
            return Check(
                "Alcance de escrita", FAIL, detail=f"HTTP {e.status} em /groups",
                remedy="O principal não é membro de nenhum workspace. Para AGIR (cancelar "
                       "refresh/jobs) ele precisa ser Admin ou Member dos workspaces monitorados. "
                       "Só para alertar, isto não é necessário.",
            )
        if n == 0:
            problems.append("principal não é membro de nenhum workspace")
        if problems:
            return Check(
                "Alcance de escrita", WARN, detail="; ".join(problems),
                remedy="Adicione o SPN como Member/Admin nos workspaces monitorados, ou mantenha "
                       "mode='observe'.",
            )
        return Check("Alcance de escrita", OK, detail=f"membro de ao menos {n} workspace(s)")

    def check_snapshot(self) -> Check:
        cfg = self.config.metrics_source
        source = build_source(cfg, self.client() if cfg.kind == "metrics_app_rest" else None)
        now = clock.now_in(self.config.timezone)
        total_items = 0
        details = []
        for cap in self.config.enabled_capacities:
            rows = source.snapshot(cap, now=now)
            total_items += len(rows)
            top = sorted(rows, key=lambda r: r.cu_seconds_today, reverse=True)[:3]
            details.append(
                f"{cap.key}: {len(rows)} itens; topo: "
                + ", ".join(f"{r.item_name}={r.cu_seconds_today:,.0f}" for r in top)
            )
        if total_items == 0:
            return Check(
                "Snapshot real", FAIL, detail="nenhum item retornado",
                remedy="O DAX rodou mas não devolveu linhas. Causas comuns: capacity_id errado, "
                       "Metrics App ainda não processou o dia corrente, ou filtro de data "
                       "incompatível com o fuso do modelo.",
            )
        return Check("Snapshot real", OK, detail=" | ".join(details))

    def check_notifiers(self) -> Check:
        configured = [
            (c.key, [n.kind for n in c.policy.notify]) for c in self.config.enabled_capacities if c.policy
        ]
        silent = [k for k, kinds in configured if not kinds or kinds == ["none"]]
        if silent:
            return Check(
                "Notificação", WARN, detail=f"sem canal: {silent}",
                remedy="Um watchdog que age sem avisar ninguém é pior que nenhum. "
                       "Configure defaults.notify com ao menos um canal.",
            )
        return Check("Notificação", OK, detail=str(dict(configured)))

    def check_enforcement_readiness(self) -> Check:
        """Enforcement sem histórico é o modo mais rápido de matar produção por engano."""
        enforcing = [c for c in self.config.enabled_capacities if c.policy and c.policy.mode == "enforce"]
        if not enforcing:
            return Check(
                "Prontidão para enforce", OK,
                detail="todas em observe — recomendado nas primeiras 2–4 semanas",
            )
        store = build_store(self.config.storage)
        try:
            warnings = []
            for cap in enforcing:
                days = cap.policy.baseline.lookback_days
                rows = store.load_intervals(
                    cap.key,
                    since=dt.date.today() - dt.timedelta(days=days),
                    until=dt.date.today() + dt.timedelta(days=1),
                )
                distinct_days = len({r.date for r in rows})
                if distinct_days < cap.policy.baseline.min_days:
                    warnings.append(
                        f"{cap.key}: só {distinct_days} dia(s) de histórico "
                        f"(min_days={cap.policy.baseline.min_days})"
                    )
            if warnings:
                return Check(
                    "Prontidão para enforce", FAIL, detail="; ".join(warnings),
                    remedy="Volte para mode='observe' até acumular histórico. Sem baseline "
                           "confiável o watchdog vai matar carga legítima.",
                )
            return Check(
                "Prontidão para enforce", OK,
                detail=f"{[c.key for c in enforcing]} com histórico suficiente",
            )
        finally:
            store.close()


def render(checks: List[Check]) -> str:
    lines = []
    for c in checks:
        lines.append(f"{c.icon} {c.name}: {c.detail}" if c.detail else f"{c.icon} {c.name}")
        if c.remedy and c.status in (FAIL, WARN):
            for rl in _wrap(c.remedy, 88):
                lines.append(f"      → {rl}")
    counts = {s: sum(1 for c in checks if c.status == s) for s in (OK, WARN, FAIL, SKIP)}
    lines.append("")
    lines.append(
        f"Resultado: {counts[OK]} ok, {counts[WARN]} aviso(s), {counts[FAIL]} erro(s), "
        f"{counts[SKIP]} não aplicável(is)."
    )
    if counts[FAIL]:
        lines.append("Corrija os erros acima antes do primeiro ciclo. Detalhes: docs/PERMISSIONS.md")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> List[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def requires_service_principal(config: WatchdogConfig) -> bool:
    return isinstance(config.auth, ServicePrincipalAuth)
