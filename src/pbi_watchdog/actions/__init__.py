"""Ações de contenção. Toda ação respeita `dry_run` e devolve `ActionResult` — nunca levanta
exceção para o runner: uma ação que falha não pode derrubar o ciclo nem impedir as demais.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Callable, Dict, Optional, Protocol

from ..models import ActionResult, Assessment
from ..rest import FABRIC_BASE, PBI_BASE, ApiError, RestClient

log = logging.getLogger(__name__)

#: Kinds de item que representam um semantic model / dataset.
DATASET_KINDS = {"Dataset", "SemanticModel", "Model"}


class Action(Protocol):
    name: str

    def execute(self, assessment: Assessment, *, dry_run: bool) -> ActionResult: ...


class CancelRefresh:
    """Cancela refreshes em andamento de um semantic model (Enhanced Refresh API).

    Só enxerga refreshes cujo status seja "Unknown" — na API do Power BI é assim que um
    refresh em andamento aparece. Refresh já concluído não é afetado.
    """

    name = "cancel_refresh"

    def __init__(self, client: RestClient) -> None:
        self.client = client

    def execute(self, a: Assessment, *, dry_run: bool) -> ActionResult:
        iv = a.interval
        if iv.item_kind not in DATASET_KINDS:
            return ActionResult(self.name, True, detail=f"ignorado: kind={iv.item_kind}")
        if not iv.workspace_id:
            return ActionResult(self.name, False, detail="workspace_id ausente no Metrics App")

        base = f"{PBI_BASE}/groups/{iv.workspace_id}/datasets/{iv.item_id}/refreshes"
        try:
            data = self.client.get(base, params={"$top": 10}, allowed_status=(404,))
        except ApiError as e:
            return ActionResult(self.name, False, detail=f"listagem falhou: {e.status}")

        running = [r for r in data.get("value", []) if r.get("status") == "Unknown"]
        if not running:
            return ActionResult(self.name, True, detail="nenhum refresh em andamento")

        targets = [r.get("requestId", "?") for r in running]
        if dry_run:
            return ActionResult(
                self.name, True, detail=f"[dry-run] cancelaria {len(targets)}", targets=targets
            )

        cancelled, failed = [], []
        for rid in targets:
            try:
                self.client.delete(f"{base}/{rid}")
                cancelled.append(rid)
            except ApiError as e:
                failed.append(f"{rid}({e.status})")
        return ActionResult(
            self.name,
            ok=not failed,
            detail=f"cancelados={len(cancelled)} falhas={failed or 0}",
            targets=cancelled,
        )


class CancelFabricJobs:
    """Cancela job instances em execução: notebooks, pipelines, dataflows Gen2, sparkjobs."""

    name = "cancel_fabric_jobs"

    def __init__(self, client: RestClient) -> None:
        self.client = client

    def execute(self, a: Assessment, *, dry_run: bool) -> ActionResult:
        iv = a.interval
        if not iv.workspace_id:
            return ActionResult(self.name, False, detail="workspace_id ausente")

        base = f"{FABRIC_BASE}/workspaces/{iv.workspace_id}/items/{iv.item_id}/jobs/instances"
        try:
            data = self.client.get(base, allowed_status=(400, 404))
        except ApiError as e:
            return ActionResult(self.name, False, detail=f"listagem falhou: {e.status}")
        if not isinstance(data, dict):
            return ActionResult(self.name, True, detail="item não expõe jobs")

        running = [
            j for j in data.get("value", []) if j.get("status") in ("InProgress", "NotStarted")
        ]
        if not running:
            return ActionResult(self.name, True, detail="nenhum job em execução")

        targets = [j.get("id", "?") for j in running]
        if dry_run:
            return ActionResult(
                self.name, True, detail=f"[dry-run] cancelaria {len(targets)}", targets=targets
            )

        cancelled, failed = [], []
        for jid in targets:
            try:
                self.client.post(f"{base}/{jid}/cancel")
                cancelled.append(jid)
            except ApiError as e:
                failed.append(f"{jid}({e.status})")
        return ActionResult(
            self.name, ok=not failed, detail=f"cancelados={len(cancelled)} falhas={failed or 0}",
            targets=cancelled,
        )


class KillXmlaSessions:
    """Derruba sessões XMLA ativas de um dataset — inclusive usuários no meio de um relatório.

    Exige XMLA read-write na capacidade e ADOMD disponível, então só funciona dentro do Fabric
    ou numa máquina com o cliente instalado. Fora desses ambientes a ação degrada para um
    resultado explicitamente falho, e o alerta continua saindo.
    """

    name = "kill_xmla_sessions"

    def __init__(self, workspace_resolver: Optional[Callable[[str], str]] = None) -> None:
        self.workspace_resolver = workspace_resolver

    def execute(self, a: Assessment, *, dry_run: bool) -> ActionResult:
        iv = a.interval
        if iv.item_kind not in DATASET_KINDS:
            return ActionResult(self.name, True, detail=f"ignorado: kind={iv.item_kind}")

        try:
            import sempy.fabric as fabric  # type: ignore
        except ImportError:
            return ActionResult(
                self.name,
                False,
                detail="indisponível: kill_xmla_sessions exige sempy/ADOMD (rode no Fabric)",
            )

        ws = iv.workspace_name
        if not ws and self.workspace_resolver:
            ws = self.workspace_resolver(iv.workspace_id)
        if not ws:
            return ActionResult(self.name, False, detail="workspace_name não resolvido")

        try:
            sessions = fabric.evaluate_dax(
                dataset=iv.item_name, workspace=ws, dax_string="EVALUATE $SYSTEM.DISCOVER_SESSIONS"
            )
        except Exception as e:
            # Qualquer falha aqui é informativa, não fatal: o alerta já saiu e o ciclo segue.
            return ActionResult(self.name, False, detail=f"DISCOVER_SESSIONS falhou: {e}")

        spids = []
        for _, row in sessions.iterrows():
            spid = row.get("SESSION_SPID") or row.get("[SESSION_SPID]")
            if spid:
                spids.append(int(spid))
        if not spids:
            return ActionResult(self.name, True, detail="nenhuma sessão ativa")
        if dry_run:
            return ActionResult(
                self.name, True, detail=f"[dry-run] mataria SPIDs {spids}", targets=[str(s) for s in spids]
            )

        killed, failed = [], []
        try:
            server = fabric.create_tom_server(readonly=False, workspace=ws)
        except Exception as e:
            return ActionResult(self.name, False, detail=f"conexão TOM falhou: {e}")
        try:
            for spid in spids:
                xmla = (
                    '<Cancel xmlns="http://schemas.microsoft.com/analysisservices/2003/engine">'
                    f"<SPID>{spid}</SPID><CancelAssociated>true</CancelAssociated></Cancel>"
                )
                try:
                    server.Execute(xmla)
                    killed.append(str(spid))
                except Exception as e:
                    failed.append(f"{spid}({e})")
        finally:
            # Falha ao desconectar não muda o resultado das sessões já derrubadas.
            with contextlib.suppress(Exception):
                server.Disconnect()
        return ActionResult(
            self.name, ok=not failed, detail=f"mortos={killed} falhas={failed}", targets=killed
        )


class NotifyAction:
    """Placeholder: a notificação é despachada pelo runner, que conhece todos os canais."""

    name = "notify"

    def execute(self, a: Assessment, *, dry_run: bool) -> ActionResult:
        return ActionResult(self.name, True, detail="")


def build_registry(client: Optional[RestClient]) -> Dict[str, Action]:
    registry: Dict[str, Action] = {"notify": NotifyAction(), "kill_xmla_sessions": KillXmlaSessions()}
    if client is not None:
        registry["cancel_refresh"] = CancelRefresh(client)
        registry["cancel_fabric_jobs"] = CancelFabricJobs(client)
    return registry


__all__ = [
    "Action",
    "ActionResult",
    "CancelFabricJobs",
    "CancelRefresh",
    "KillXmlaSessions",
    "NotifyAction",
    "build_registry",
]
