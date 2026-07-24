# Do notebook para a biblioteca

Mapa entre o `capacity_watchdog.ipynb` original e a lib, para conferir que nada se perdeu.

| Notebook | Biblioteca |
|---|---|
| `MODE = "OBSERVE"` | `defaults.mode` — agora por capacidade via `overrides.mode` |
| `CAPACITIES = {nome: guid}` | `capacities[]` com `key`, `id`, `sku`, política própria |
| `METRICS_WORKSPACE` / `METRICS_DATASET` | `metrics_source.workspace_id` / `dataset_id` |
| `TIER_ALERT` / `TIER_KILL_BG` / `TIER_KILL_SESSION` | `thresholds.alert` / `.throttle` / `.kill` |
| `ALLOWLIST_ITEM_IDS` | `protect.item_ids` + `workspace_ids`, `item_kinds`, `name_patterns` |
| `MIN_CU_SECONDS` | `guards.min_cu_seconds` |
| `MIN_BASELINE_DAYS` | `baseline.min_days` |
| `INTERVAL_MIN` | `defaults.interval_minutes` (agora também normaliza atrasos) |
| `SNAPSHOT_TABLE` / `EVENTS_TABLE` | `storage` |
| `TEAMS_WEBHOOK_URL` | `notify[].url`, com `min_tier` por canal |
| Cell 3 (explorar modelo) | `pbi-watchdog inspect-model` + detecção automática de perfil |
| Cell 4 (DAX do snapshot) | `sources/profiles.py`, ou `metrics_source.dax_override` |
| Cell 5 (diff + baseline Spark) | `core/baseline.py`, Python puro e testado |
| Cell 6 (detecção) | `core/detect.py` |
| Cell 7 (kills) | `actions/` — cada uma devolve resultado em vez de levantar exceção |
| Cell 8 (orquestração) | `runner.py` |
| Calibração manual no `watchdog_events` | `pbi-watchdog calibrate` |

## O que mudou de comportamento

Nem tudo é tradução; algumas coisas o notebook fazia de um jeito que não sobrevive a
produção.

**Baseline por média virou mediana com trim.** No notebook, `F.avg` sobre 7 dias faz um
incidente da semana passada elevar a baseline e mascarar o incidente desta semana. A mediana
com `trim_top_percent` resolve isso.

**Intervalos são normalizados pela duração real.** O notebook assumia que todo intervalo tem
`INTERVAL_MIN` minutos. Quando o agendador atrasa — e ele atrasa — um intervalo de 45 minutos
aparecia como um pico de 3x. Agora o consumo é reescalado antes de comparar.

**A janela do `lag()` era global, não por execução.** A Cell 5 recalculava os intervalos de
todo o histórico a cada run via `Window.partitionBy(...).orderBy("ts")`. Além do custo, isso
significava que corrigir um snapshot ruim exigia reprocessar tudo. Agora o intervalo é
calculado uma vez, no momento em que os dois snapshots existem, e persistido.

**Ação exige violação sustentada.** O notebook agia no primeiro ciclo acima do threshold.
`guards.consecutive_breaches: 2` exige que a anomalia dure dois ciclos — é a trava que mais
reduz falso positivo, e a que mais custa caro não ter.

**Existe um circuit breaker.** O notebook iterava sobre todas as anomalias e matava todas.
Se 40 itens ficam anômalos ao mesmo tempo, a causa é sistêmica e matar os 40 transforma um
problema de performance num apagão. `guards.max_actions_per_run` derruba o ciclo inteiro para
alerta nesse cenário.

**Falha de ação não interrompe o ciclo.** No notebook, uma exceção em `cancel_refreshes`
abortava o loop e os itens seguintes não eram avaliados. Agora cada ação devolve um
`ActionResult` e o erro entra na auditoria.

**A auditoria registra o que NÃO foi feito.** `Event.suppressions` diz por que uma anomalia
detectada não virou ação. Sem isso é impossível calibrar — você vê o que aconteceu, mas não
o que quase aconteceu.

## Rodando a lib dentro do mesmo notebook

Se preferir manter o agendamento pelo Fabric, o notebook vira três células:

```python
%pip install pbi-watchdog
```

```python
from pbi_watchdog import WatchdogConfig, Watchdog

config = WatchdogConfig.from_dict({
    "version": 1,
    "timezone": "America/Sao_Paulo",
    "auth": {"kind": "notebook"},
    "metrics_source": {
        "kind": "metrics_app_sempy",
        "workspace_name": "Microsoft Fabric Capacity Metrics",
        "dataset_name": "Fabric Capacity Metrics",
    },
    "storage": {"kind": "delta", "table_prefix": "watchdog_"},
    "defaults": {
        "mode": "observe",
        "interval_minutes": 15,
        "notify": [{"kind": "teams", "url": TEAMS_WEBHOOK_URL}],
    },
    "capacities": [
        {"key": "F128_PROD", "id": "<guid>", "sku": "F128"},
        {"key": "F64_SANDBOX", "id": "<guid>", "sku": "F64"},
    ],
})
```

```python
wd = Watchdog(config)
for s in wd.run_once():
    print(s.capacity_key, s.items_scanned, s.anomalies, s.actions_taken)
    for e in s.events:
        print(" ", e.tier, "→", e.effective_tier, e.item_name, round(e.ratio or 0, 2), e.suppressions)
wd.close()
```

O agendamento continua sendo o do Fabric, a cada 15 minutos, batendo com `interval_minutes`.
