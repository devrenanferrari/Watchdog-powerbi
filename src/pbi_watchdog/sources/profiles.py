"""Perfis de DAX para o Fabric Capacity Metrics App.

O modelo do app muda de nome entre versões, e é a causa número um de "funcionou na minha
tenant e quebrou na sua". Em vez de embutir um DAX fixo, declaramos perfis com as tabelas
e colunas que cada um exige e detectamos qual bate, consultando o próprio modelo via
`INFO.TABLES()` / `INFO.COLUMNS()`.

Se nenhum perfil bater, `metrics_source.dax_override` na config é a saída — a query só
precisa devolver as colunas de `REQUIRED_COLUMNS`, nesta ordem de nomes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

#: Colunas que o resto da lib espera, independentemente do perfil.
REQUIRED_COLUMNS = [
    "item_id",
    "item_name",
    "item_kind",
    "workspace_id",
    "workspace_name",
    "cu_seconds_today",
]


@dataclass(frozen=True)
class MetricsProfile:
    name: str
    description: str
    #: Tabelas que precisam existir no modelo para o perfil ser aplicável.
    requires_tables: List[str]
    #: Colunas "Tabela[Coluna]" que precisam existir.
    requires_columns: List[str]
    #: DAX com placeholder {capacity_id}.
    dax: str
    #: Mapeia coluna do resultado (como o executeQueries devolve) -> nome canônico.
    column_map: Dict[str, str]


_MAP_V1 = {
    "Items[ItemId]": "item_id",
    "Items[ItemName]": "item_name",
    "Items[ItemKind]": "item_kind",
    "Items[WorkspaceId]": "workspace_id",
    "Items[WorkspaceName]": "workspace_name",
    "[cu_seconds_today]": "cu_seconds_today",
}

FABRIC_METRICS_V1 = MetricsProfile(
    name="fabric_metrics_v1",
    description="Fabric Capacity Metrics App — layout com MetricsByItemandOperationandDay.",
    requires_tables=["Items", "Capacities", "MetricsByItemandOperationandDay"],
    requires_columns=[
        "Items[ItemId]",
        "Items[WorkspaceId]",
        "Capacities[capacityId]",
        "MetricsByItemandOperationandDay[Date]",
    ],
    dax="""
EVALUATE
SUMMARIZECOLUMNS(
    Items[ItemId],
    Items[ItemName],
    Items[ItemKind],
    Items[WorkspaceId],
    Items[WorkspaceName],
    TREATAS({{"{capacity_id}"}}, Capacities[capacityId]),
    FILTER(
        VALUES(MetricsByItemandOperationandDay[Date]),
        MetricsByItemandOperationandDay[Date] = TODAY()
    ),
    "cu_seconds_today", CALCULATE(SUM(MetricsByItemandOperationandDay[sum_CU]))
)
""".strip(),
    column_map=_MAP_V1,
)

FABRIC_METRICS_V2 = MetricsProfile(
    name="fabric_metrics_v2",
    description="Layout com a medida pré-definida [CU (s)] em vez de SUM(sum_CU).",
    requires_tables=["Items", "Capacities", "MetricsByItemandOperationandDay"],
    requires_columns=[
        "Items[ItemId]",
        "Capacities[capacityId]",
        "MetricsByItemandOperationandDay[Date]",
    ],
    dax="""
EVALUATE
SUMMARIZECOLUMNS(
    Items[ItemId],
    Items[ItemName],
    Items[ItemKind],
    Items[WorkspaceId],
    Items[WorkspaceName],
    TREATAS({{"{capacity_id}"}}, Capacities[capacityId]),
    FILTER(
        VALUES(MetricsByItemandOperationandDay[Date]),
        MetricsByItemandOperationandDay[Date] = TODAY()
    ),
    "cu_seconds_today", [CU (s)]
)
""".strip(),
    column_map=_MAP_V1,
)

FABRIC_METRICS_TIMEPOINTS = MetricsProfile(
    name="fabric_metrics_timepoints",
    description=(
        "Layout com TimePoints: soma o CU do dia corrente a partir da granularidade de 30s. "
        "Mais preciso quando disponível, porém mais pesado no modelo."
    ),
    requires_tables=["Items", "Capacities", "TimePoints"],
    requires_columns=["Items[ItemId]", "Capacities[capacityId]", "TimePoints[Date]"],
    dax="""
EVALUATE
SUMMARIZECOLUMNS(
    Items[ItemId],
    Items[ItemName],
    Items[ItemKind],
    Items[WorkspaceId],
    Items[WorkspaceName],
    TREATAS({{"{capacity_id}"}}, Capacities[capacityId]),
    FILTER(VALUES(TimePoints[Date]), TimePoints[Date] = TODAY()),
    "cu_seconds_today", [CU (s)]
)
""".strip(),
    column_map=_MAP_V1,
)

PROFILES: Dict[str, MetricsProfile] = {
    p.name: p for p in (FABRIC_METRICS_V1, FABRIC_METRICS_V2, FABRIC_METRICS_TIMEPOINTS)
}

#: Ordem de tentativa no modo "auto".
PROBE_ORDER = ["fabric_metrics_v1", "fabric_metrics_v2", "fabric_metrics_timepoints"]

#: Introspecção do modelo via funções DAX INFO.*. Duas queries em vez de um join:
#: SELECTCOLUMNS quebra a lineage que NATURALLEFTOUTERJOIN exige, então o join
#: acontece em Python (`join_tables_and_columns`).
DAX_LIST_TABLES = 'EVALUATE SELECTCOLUMNS(INFO.TABLES(), "tid", [ID], "name", [Name])'
DAX_LIST_COLUMNS = 'EVALUATE SELECTCOLUMNS(INFO.COLUMNS(), "tid", [TableID], "col", [ExplicitName])'


def _cell(row: dict, *names: str):
    """O executeQueries devolve as chaves com colchetes; o sempy, sem. Aceita as duas formas."""
    for n in names:
        for candidate in (f"[{n}]", n):
            if candidate in row:
                return row[candidate]
    return None


def join_tables_and_columns(table_rows: List[dict], column_rows: List[dict]) -> List[str]:
    """Monta a lista "Tabela[Coluna]" a partir das duas queries de introspecção."""
    names_by_id = {}
    for r in table_rows:
        tid, name = _cell(r, "tid"), _cell(r, "name")
        if tid is not None and name:
            names_by_id[str(tid)] = str(name)

    out = []
    for r in column_rows:
        tid, col = _cell(r, "tid"), _cell(r, "col")
        table = names_by_id.get(str(tid))
        if table and col:
            out.append(f"{table}[{col}]")
    return out


def table_names(table_rows: List[dict]) -> List[str]:
    return [str(_cell(r, "name")) for r in table_rows if _cell(r, "name")]


def normalize_row(row: dict, profile: MetricsProfile) -> dict:
    """Traduz uma linha do executeQueries para as chaves canônicas.

    Aceita variações de aspas/colchetes que a API às vezes devolve.
    """
    out: dict = {}
    for raw_key, value in row.items():
        canonical = profile.column_map.get(raw_key)
        if canonical is None:
            stripped = raw_key.replace("[", "").replace("]", "").strip()
            for k, v in profile.column_map.items():
                if k.replace("[", "").replace("]", "").strip().lower() == stripped.lower():
                    canonical = v
                    break
        if canonical:
            out[canonical] = value
    return out


def missing_requirements(
    profile: MetricsProfile, tables: List[str], columns: List[str]
) -> List[str]:
    lower_tables = {t.lower() for t in tables}
    lower_columns = {c.lower() for c in columns}
    missing = [f"tabela {t}" for t in profile.requires_tables if t.lower() not in lower_tables]
    missing += [f"coluna {c}" for c in profile.requires_columns if c.lower() not in lower_columns]
    return missing


def pick_profile(tables: List[str], columns: List[str]) -> Optional[MetricsProfile]:
    for name in PROBE_ORDER:
        p = PROFILES[name]
        if not missing_requirements(p, tables, columns):
            return p
    return None
