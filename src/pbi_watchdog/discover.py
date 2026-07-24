"""Descoberta do Capacity Metrics App e das capacidades.

O nome do workspace do Metrics App varia por versão do app, idioma do tenant e renomeação
local — "Workspace 'Microsoft Fabric Capacity Metrics' not found" é o primeiro erro que
quase todo mundo encontra. Em vez de exigir que o usuário adivinhe o nome, aqui a lib
procura por ele nos dois runtimes: REST (fora do Fabric) e sempy (dentro).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from .rest import PBI_BASE, ApiError, RestClient

log = logging.getLogger(__name__)

#: Sinais no nome do workspace/dataset que indicam um Capacity Metrics App.
_HINTS = re.compile(r"metric|capacity|capacidade|métrica|metrica", re.IGNORECASE)

#: Nomes já vistos em tenants reais, em ordem de probabilidade. Servem de palpite quando
#: a listagem não está disponível.
KNOWN_WORKSPACE_NAMES = [
    "Microsoft Fabric Capacity Metrics",
    "Fabric Capacity Metrics",
    "Microsoft Fabric Capacity Metrics App",
    "Power BI Premium Capacity Metrics",
    "Capacity Metrics",
]

KNOWN_DATASET_NAMES = [
    "Fabric Capacity Metrics",
    "Capacity Metrics",
    "Microsoft Fabric Capacity Metrics",
]


@dataclass
class MetricsCandidate:
    workspace_id: str
    workspace_name: str
    dataset_id: str
    dataset_name: str
    score: int = 0

    def as_config(self, *, kind: str = "metrics_app_rest") -> Dict[str, Any]:
        """Devolve o bloco `metrics_source` pronto para colar na config."""
        if kind == "metrics_app_sempy":
            return {
                "kind": "metrics_app_sempy",
                "workspace_name": self.workspace_name,
                "dataset_name": self.dataset_name,
                "profile": "auto",
            }
        return {
            "kind": "metrics_app_rest",
            "workspace_id": self.workspace_id,
            "dataset_id": self.dataset_id,
            "profile": "auto",
        }

    def __str__(self) -> str:
        return (
            f"{self.workspace_name} / {self.dataset_name}\n"
            f"    workspace_id: {self.workspace_id}\n"
            f"    dataset_id:   {self.dataset_id}"
        )


def _score(workspace_name: str, dataset_name: str) -> int:
    """Prioriza o que mais se parece com o Metrics App oficial."""
    score = 0
    if _HINTS.search(workspace_name or ""):
        score += 2
    if _HINTS.search(dataset_name or ""):
        score += 2
    if (workspace_name or "") in KNOWN_WORKSPACE_NAMES:
        score += 5
    if (dataset_name or "") in KNOWN_DATASET_NAMES:
        score += 5
    if re.search(r"fabric", workspace_name or "", re.IGNORECASE):
        score += 1
    return score


def _pick(candidates: List[MetricsCandidate]) -> List[MetricsCandidate]:
    return sorted(candidates, key=lambda c: c.score, reverse=True)


# --------------------------------------------------------------------------- sempy


def _column(df, *names: str):
    """O sempy mudou nomes de coluna entre versões ('Id' vs 'Dataset Id'); aceita variantes."""
    for n in names:
        if n in df.columns:
            return df[n]
    lowered = {c.lower(): c for c in df.columns}
    for n in names:
        if n.lower() in lowered:
            return df[lowered[n.lower()]]
    return None


def find_metrics_app_sempy(*, include_all: bool = False) -> List[MetricsCandidate]:
    """Procura o Metrics App usando sempy — para uso dentro de um notebook Fabric.

    Com `include_all=True`, devolve todos os workspaces/datasets visíveis em vez de só os
    candidatos, para o caso de o app ter sido renomeado para algo irreconhecível.
    """
    try:
        import sempy.fabric as fabric  # type: ignore
    except ImportError as e:  # pragma: no cover - só existe dentro do Fabric
        raise RuntimeError(
            "find_metrics_app_sempy() exige o sempy, que só existe no runtime Fabric. "
            "Fora dele use find_metrics_app_rest(client)."
        ) from e

    workspaces = fabric.list_workspaces()
    ws_ids = _column(workspaces, "Id", "Workspace Id")
    ws_names = _column(workspaces, "Name", "Workspace Name")
    if ws_ids is None or ws_names is None:
        raise RuntimeError(
            f"Não reconheci as colunas de list_workspaces(): {list(workspaces.columns)}. "
            "Reporte isso — enquanto isso, liste manualmente com fabric.list_workspaces()."
        )

    out: List[MetricsCandidate] = []
    for ws_id, ws_name in zip(ws_ids, ws_names):
        if not include_all and not _HINTS.search(str(ws_name or "")):
            continue
        try:
            datasets = fabric.list_datasets(workspace=str(ws_name))
        except Exception as e:
            log.debug("Não consegui listar datasets de %s: %s", ws_name, e)
            continue
        if datasets is None or len(datasets) == 0:
            continue

        ds_ids = _column(datasets, "Dataset Id", "Id", "Dataset ID")
        ds_names = _column(datasets, "Dataset Name", "Name")
        if ds_names is None:
            continue
        ids = ds_ids if ds_ids is not None else ["" for _ in range(len(ds_names))]

        for ds_id, ds_name in zip(ids, ds_names):
            out.append(
                MetricsCandidate(
                    workspace_id=str(ws_id),
                    workspace_name=str(ws_name),
                    dataset_id=str(ds_id),
                    dataset_name=str(ds_name),
                    score=_score(str(ws_name), str(ds_name)),
                )
            )
    return _pick(out)


def list_capacities_sempy() -> List[Dict[str, str]]:
    """Lê as capacidades a partir do próprio modelo do Metrics App — é o caminho que não
    exige permissão de admin API."""
    import sempy.fabric as fabric  # type: ignore

    candidates = find_metrics_app_sempy()
    if not candidates:
        raise RuntimeError(
            "Nenhum Capacity Metrics App encontrado. Rode "
            "find_metrics_app_sempy(include_all=True) para ver todos os workspaces visíveis."
        )
    best = candidates[0]
    df = fabric.evaluate_dax(
        dataset=best.dataset_name,
        workspace=best.workspace_name,
        dax_string=(
            'EVALUATE SELECTCOLUMNS(Capacities, "id", [capacityId], "nome", [capacityName])'
        ),
    )
    out = []
    for _, row in df.iterrows():
        values = list(row.values)
        if len(values) >= 2:
            out.append({"id": str(values[0]), "name": str(values[1])})
    return out


# --------------------------------------------------------------------------- REST


def find_metrics_app_rest(
    client: RestClient, *, include_all: bool = False, max_workspaces: int = 200
) -> List[MetricsCandidate]:
    """Mesma busca via REST, para quem roda fora do Fabric."""
    groups = client.get(f"{PBI_BASE}/groups", params={"$top": max_workspaces})
    out: List[MetricsCandidate] = []
    for g in groups.get("value", []):
        name = g.get("name", "")
        if not include_all and not _HINTS.search(name):
            continue
        try:
            datasets = client.get(f"{PBI_BASE}/groups/{g['id']}/datasets")
        except ApiError as e:
            log.debug("Sem acesso aos datasets de %s: HTTP %s", name, e.status)
            continue
        for d in datasets.get("value", []):
            out.append(
                MetricsCandidate(
                    workspace_id=str(g.get("id", "")),
                    workspace_name=str(name),
                    dataset_id=str(d.get("id", "")),
                    dataset_name=str(d.get("name", "")),
                    score=_score(name, str(d.get("name", ""))),
                )
            )
    return _pick(out)


# --------------------------------------------------------------------------- render


def render(candidates: Sequence[MetricsCandidate], *, kind: str = "metrics_app_rest") -> str:
    if not candidates:
        return (
            "Nenhum candidato a Capacity Metrics App encontrado.\n"
            "  1. Confirme que o app está instalado (AppSource > Microsoft Fabric Capacity Metrics).\n"
            "  2. Confirme que a identidade que executa enxerga o workspace dele.\n"
            "  3. Se o workspace foi renomeado, repita com include_all=True."
        )
    lines = [f"{len(candidates)} candidato(s), do mais provável ao menos:", ""]
    for i, c in enumerate(candidates, 1):
        lines.append(f"  {i}. {c}   (score {c.score})")
    lines += ["", "Bloco metrics_source para o melhor candidato:", ""]
    best = candidates[0].as_config(kind=kind)
    for k, v in best.items():
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def suggest_config(candidates: Sequence[MetricsCandidate], *, kind: str) -> Optional[Dict[str, Any]]:
    return candidates[0].as_config(kind=kind) if candidates else None
