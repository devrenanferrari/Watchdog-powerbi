"""Núcleo de decisão: funções puras sobre dataclasses, sem I/O.

`baseline` deriva consumo e expectativa a partir do histórico; `detect` transforma isso
em veredito. Ambos são testáveis sem tenant, sem rede e sem Spark — é onde a lógica de
produto mora, e por isso é o que os testes cobrem.
"""

from . import baseline, detect

__all__ = ["baseline", "detect"]
