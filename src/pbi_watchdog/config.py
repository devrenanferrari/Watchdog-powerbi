"""Schema de configuração. É o contrato com o usuário da biblioteca — se algo é obrigatório,
é obrigatório aqui, com mensagem de erro que diz o que fazer.

Toda string suporta interpolação de ambiente: ``${VAR}`` ou ``${VAR:-default}``.
Segredos NÃO devem ficar no YAML; use ``${...}``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    """Substitui ${VAR} / ${VAR:-default} recursivamente em strings, listas e dicts."""
    if isinstance(value, str):

        def repl(m: re.Match) -> str:
            var, default = m.group(1), m.group(2)
            env = os.environ.get(var)
            if env is None:
                if default is None:
                    raise ValueError(
                        f"Variável de ambiente '{var}' referenciada na config mas não definida. "
                        f"Defina-a ou use ${{{var}:-valor_padrao}}."
                    )
                return default
            return env

        return _ENV_RE.sub(repl, value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")  # typo em chave é erro, não silêncio


# --------------------------------------------------------------------------- auth


class ServicePrincipalAuth(Base):
    kind: Literal["service_principal"] = "service_principal"
    tenant_id: str
    client_id: str
    client_secret: Optional[str] = None
    certificate_path: Optional[str] = None
    certificate_thumbprint: Optional[str] = None

    @model_validator(mode="after")
    def _need_a_credential(self) -> "ServicePrincipalAuth":
        has_secret = bool(self.client_secret)
        has_cert = bool(self.certificate_path and self.certificate_thumbprint)
        if not (has_secret or has_cert):
            raise ValueError(
                "auth.service_principal exige 'client_secret' OU o par "
                "'certificate_path' + 'certificate_thumbprint'."
            )
        return self


class ManagedIdentityAuth(Base):
    kind: Literal["managed_identity"] = "managed_identity"
    client_id: Optional[str] = None  # user-assigned; omita para system-assigned


class NotebookAuth(Base):
    """Usa a identidade que executa o notebook Fabric (notebookutils.credentials)."""

    kind: Literal["notebook"] = "notebook"


AuthConfig = Union[ServicePrincipalAuth, ManagedIdentityAuth, NotebookAuth]


# --------------------------------------------------------------------------- fonte de métricas


class MetricsSourceConfig(Base):
    kind: Literal["metrics_app_rest", "metrics_app_sempy", "fake"] = "metrics_app_rest"
    #: Workspace/dataset onde o Fabric Capacity Metrics App está instalado.
    workspace_id: Optional[str] = None
    dataset_id: Optional[str] = None
    workspace_name: Optional[str] = None
    dataset_name: Optional[str] = None
    #: Nome do profile de DAX (ver sources/profiles.py) ou "auto" para detectar no primeiro run.
    profile: str = "auto"
    #: DAX cru, se o seu Metrics App foi customizado. Recebe {capacity_id} por format().
    dax_override: Optional[str] = None
    timeout_seconds: int = 120

    @model_validator(mode="after")
    def _need_identifiers(self) -> "MetricsSourceConfig":
        if self.kind == "fake":
            return self
        if self.kind == "metrics_app_rest" and not self.dataset_id:
            raise ValueError(
                "metrics_source.kind='metrics_app_rest' exige 'dataset_id' (GUID do dataset "
                "do Capacity Metrics App). 'workspace_id' é opcional: omita-o quando o app "
                "estiver instalado no 'Meu workspace' (URL com /groups/me/apps/...), que é "
                "como o AppSource o instala. "
                "Rode `pbi-watchdog discover --metrics` para descobrir os GUIDs."
            )
        if self.kind == "metrics_app_sempy" and not (self.workspace_name and self.dataset_name):
            raise ValueError(
                "metrics_source.kind='metrics_app_sempy' exige 'workspace_name' e 'dataset_name'."
            )
        return self


# --------------------------------------------------------------------------- storage


class SqliteStorage(Base):
    kind: Literal["sqlite"] = "sqlite"
    path: str = "./watchdog.db"


class DeltaStorage(Base):
    kind: Literal["delta"] = "delta"
    #: Prefixo das tabelas no Lakehouse anexado (requer runtime Spark/Fabric).
    table_prefix: str = "watchdog_"


#: Outros backends: implemente o protocolo `storage.StateStore` e registre em
#: `storage.build_store`. O contrato tem 9 métodos e nenhum deles depende do resto da lib.
StorageConfig = Union[SqliteStorage, DeltaStorage]


# --------------------------------------------------------------------------- políticas


class BaselineConfig(Base):
    lookback_days: int = Field(7, ge=1, le=90)
    #: hour_of_day = mesma hora em qualquer dia; hour_of_week separa seg-14h de dom-14h.
    bucket: Literal["hour_of_day", "hour_of_week"] = "hour_of_day"
    min_days: int = Field(4, ge=1, description="Dias distintos de histórico exigidos no bucket.")
    method: Literal["mean", "median", "p75", "p90"] = "median"
    #: Descarta o topo N% das amostras antes de agregar (evita baseline inflada por incidentes).
    trim_top_percent: float = Field(10.0, ge=0, le=50)


class Thresholds(Base):
    alert: float = Field(1.2, gt=1.0)
    throttle: float = Field(1.5, gt=1.0)
    kill: float = Field(1.8, gt=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> "Thresholds":
        if not (self.alert <= self.throttle <= self.kill):
            raise ValueError("thresholds devem ser crescentes: alert <= throttle <= kill.")
        return self


class Guards(Base):
    """Travas de segurança. São o que separa um watchdog de um gerador de incidentes."""

    #: Piso absoluto de consumo no intervalo. Item ocioso que sobe de 1 para 3 CU·s é 3x, e irrelevante.
    min_cu_seconds: float = Field(300.0, ge=0)
    #: Quantos ciclos consecutivos acima do tier antes de agir. 1 = age no primeiro.
    consecutive_breaches: int = Field(2, ge=1, le=10)
    #: Após agir sobre um item, ignora-o por N minutos (evita matar refresh em loop de retry).
    cooldown_minutes: int = Field(60, ge=0)
    #: Circuit breaker: se um ciclo quiser agir em mais itens que isto, não age em nenhum e alerta.
    max_actions_per_run: int = Field(5, ge=1)
    #: Descarta intervalos cuja duração destoe de `interval_minutes` por mais que este fator.
    #: Um intervalo de 12h (watchdog parado, gap no agendador) não é comparável a um de 15 min:
    #: reescalar assume consumo uniforme, o que é falso justamente quando houve pico.
    max_interval_stretch: float = Field(3.0, gt=1.0)
    #: Se o consumo TOTAL da capacidade estiver abaixo disto (% do limite), não mata nada —
    #: um item pode estar 3x a baseline dele e a capacidade estar folgada.
    min_capacity_utilization_percent: Optional[float] = Field(None, ge=0, le=100)


class FreezeWindow(Base):
    """Janela em que o enforcement é desligado (fechamento contábil, madrugada de carga)."""

    name: str
    days: List[int] = Field(default_factory=lambda: list(range(7)), description="0=segunda .. 6=domingo")
    start_hour: int = Field(0, ge=0, le=23)
    end_hour: int = Field(24, ge=1, le=24)
    #: Dias do mês (1-31); use para fechamento. Vazio = todos.
    days_of_month: List[int] = Field(default_factory=list)

    @field_validator("days")
    @classmethod
    def _valid_days(cls, v: List[int]) -> List[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("freeze_windows.days aceita 0..6 (0=segunda).")
        return v


class ProtectConfig(Base):
    """Itens que alertam mas nunca sofrem ação. Regulatório, executivo, missão crítica."""

    item_ids: List[str] = Field(default_factory=list)
    workspace_ids: List[str] = Field(default_factory=list)
    item_kinds: List[str] = Field(default_factory=list)
    #: Regex aplicado ao nome do item (case-insensitive).
    name_patterns: List[str] = Field(default_factory=list)


class NotifierConfig(Base):
    kind: Literal["teams", "slack", "webhook", "console", "none"]
    url: Optional[str] = None
    #: Só notifica deste tier para cima.
    min_tier: Literal["alert", "throttle", "kill"] = "alert"
    timeout_seconds: int = 10

    @model_validator(mode="after")
    def _need_url(self) -> "NotifierConfig":
        if self.kind in ("teams", "slack", "webhook") and not self.url:
            raise ValueError(f"notify.kind='{self.kind}' exige 'url'.")
        return self


ActionName = Literal[
    "notify",
    "cancel_refresh",
    "cancel_fabric_jobs",
    "kill_xmla_sessions",
]


class ActionsConfig(Base):
    alert: List[ActionName] = Field(default_factory=lambda: ["notify"])
    throttle: List[ActionName] = Field(
        default_factory=lambda: ["notify", "cancel_refresh", "cancel_fabric_jobs"]
    )
    kill: List[ActionName] = Field(
        default_factory=lambda: ["notify", "cancel_refresh", "cancel_fabric_jobs", "kill_xmla_sessions"]
    )


class PolicyConfig(Base):
    """Bloco reaproveitável: vira `defaults` e pode ser sobrescrito por capacidade."""

    mode: Literal["observe", "enforce"] = "observe"
    interval_minutes: int = Field(15, ge=1, le=1440)
    baseline: BaselineConfig = Field(default_factory=BaselineConfig)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    guards: Guards = Field(default_factory=Guards)
    actions: ActionsConfig = Field(default_factory=ActionsConfig)
    protect: ProtectConfig = Field(default_factory=ProtectConfig)
    freeze_windows: List[FreezeWindow] = Field(default_factory=list)
    notify: List[NotifierConfig] = Field(default_factory=list)


class CapacityConfig(Base):
    """Uma capacidade monitorada. `key` é o nome amigável usado em logs e alertas."""

    key: str
    id: str
    enabled: bool = True
    sku: Optional[str] = None
    description: Optional[str] = None
    #: Qualquer campo de PolicyConfig pode ser sobrescrito aqui (merge raso por bloco).
    overrides: Dict[str, Any] = Field(default_factory=dict)

    #: Preenchido por WatchdogConfig.resolve() — política efetiva desta capacidade.
    policy: Optional[PolicyConfig] = None


class WatchdogConfig(Base):
    version: Literal[1] = 1
    auth: AuthConfig = Field(discriminator="kind")
    metrics_source: MetricsSourceConfig
    storage: StorageConfig = Field(default_factory=SqliteStorage, discriminator="kind")
    defaults: PolicyConfig = Field(default_factory=PolicyConfig)
    capacities: List[CapacityConfig]
    #: Timezone IANA para buckets de baseline e freeze windows. UTC se omitido.
    timezone: str = "UTC"

    @field_validator("capacities")
    @classmethod
    def _unique_keys(cls, v: List[CapacityConfig]) -> List[CapacityConfig]:
        if not v:
            raise ValueError("Defina ao menos uma capacidade em 'capacities'.")
        keys = [c.key for c in v]
        dupes = {k for k in keys if keys.count(k) > 1}
        if dupes:
            raise ValueError(f"Chaves de capacidade duplicadas: {sorted(dupes)}")
        return v

    @model_validator(mode="after")
    def _resolve_policies(self) -> "WatchdogConfig":
        for cap in self.capacities:
            cap.policy = _merge_policy(self.defaults, cap.overrides)
        return self

    # ------------------------------------------------------------------ carga

    @classmethod
    def from_file(cls, path: Union[str, Path]) -> "WatchdogConfig":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"Config não encontrada: {p}. Rode `pbi-watchdog init` para gerar um template."
            )
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "WatchdogConfig":
        return cls.model_validate(_expand(raw))

    def capacity(self, key: str) -> CapacityConfig:
        for c in self.capacities:
            if c.key == key:
                return c
        raise KeyError(
            f"Capacidade '{key}' não existe na config. "
            f"Disponíveis: {[c.key for c in self.capacities]}"
        )

    @property
    def enabled_capacities(self) -> List[CapacityConfig]:
        return [c for c in self.capacities if c.enabled]


def _merge_policy(base: PolicyConfig, overrides: Dict[str, Any]) -> PolicyConfig:
    """Merge de um nível: `overrides={'thresholds': {'alert': 1.1}}` preserva throttle/kill do default."""
    merged = base.model_dump()
    for key, value in (overrides or {}).items():
        if key not in merged:
            raise ValueError(
                f"overrides.{key} não é um campo de política válido. "
                f"Campos: {sorted(PolicyConfig.model_fields)}"
            )
        if isinstance(value, dict) and isinstance(merged[key], dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return PolicyConfig.model_validate(merged)
