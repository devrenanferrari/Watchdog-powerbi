"""Relógio. Um único ponto para obter "agora", para que os testes possam controlá-lo.

Todos os timestamps da lib são *naive* e representam o fuso de `config.timezone`. Misturar
naive e aware nas comparações de cooldown e baseline é uma fonte clássica de bug, então a
conversão acontece aqui, uma vez, e o resto do código só vê datetimes ingênuos.
"""

from __future__ import annotations

import datetime as dt
import logging

log = logging.getLogger(__name__)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def now_in(timezone: str) -> dt.datetime:
    """Hora local do fuso informado, sem tzinfo.

    Buckets de baseline e freeze windows são conceitos de negócio: "a média das 14h" precisa
    ser o horário de quem usa o relatório, não UTC.
    """
    if not timezone or timezone.upper() == "UTC":
        return utcnow()
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo(timezone)).replace(tzinfo=None, microsecond=0)
    except Exception:
        log.warning("Timezone '%s' indisponível; usando UTC.", timezone)
        return utcnow()
