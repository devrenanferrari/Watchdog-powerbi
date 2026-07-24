# pbi-watchdog

Watchdog de capacidade para Power BI / Microsoft Fabric. Detecta itens consumindo CU muito
acima do próprio padrão histórico e reage em degraus — alertar, cancelar carga em background,
derrubar sessões — com travas de segurança pensadas para organizações com **várias
capacidades e políticas diferentes por capacidade**.

Roda em qualquer lugar: notebook Fabric, Azure Function, container, GitHub Actions ou laptop.
O núcleo é Python puro sobre as APIs REST; Spark e `sempy` são opcionais.

---

## Em 5 minutos

```bash
pip install pbi-watchdog

pbi-watchdog init                      # gera watchdog.yaml comentado
export PBI_CLIENT_ID=...  PBI_CLIENT_SECRET=...
pbi-watchdog discover --capacities     # descobre os GUIDs das capacidades
pbi-watchdog discover --metrics        # acha o dataset do Capacity Metrics App
pbi-watchdog doctor --deep             # diz exatamente o que ainda falta
pbi-watchdog run --dry-run -v          # primeiro ciclo, sem agir
```

Depois agende `pbi-watchdog run` a cada 15 minutos.

---

## Como funciona

A cada ciclo, por capacidade:

```
snapshot  →  intervalo  →  baseline  →  avaliação  →  travas  →  ação  →  auditoria
```

1. **Snapshot** — lê do Capacity Metrics App o CU acumulado do dia por item.
2. **Intervalo** — subtrai o snapshot anterior. O consumo do período é o que interessa,
   e é normalizado para `interval_minutes`: se o agendador atrasar, um intervalo de 45 min
   não vira um falso pico de 3x.
3. **Baseline** — mediana do consumo daquele item, naquele mesmo bucket horário, nos
   últimos N dias, aparando o topo 10% para que um incidente passado não vire "normal".
4. **Avaliação** — `consumo ÷ baseline` cai num degrau: `alert` / `throttle` / `kill`.
5. **Travas** — o que separa um watchdog de um gerador de incidentes (abaixo).
6. **Ação** — cancela refreshes, cancela jobs Fabric, derruba sessões XMLA.
7. **Auditoria** — cada decisão vira um `Event`, **inclusive o que se decidiu não fazer**
   e por quê.

### As travas de segurança

Um detector de anomalias com poder de matar processos precisa de mais desconfiança do que
um detector que só alerta. Todas são configuráveis, e nenhuma delas suprime o **alerta** —
só limitam a **ação**:

| Trava | O que evita |
|---|---|
| `min_cu_seconds` | Item ocioso que sai de 1 para 3 CU·s é "3x" e irrelevante |
| `consecutive_breaches` | Pico instantâneo. Com 2, exige anomalia sustentada por 2 ciclos |
| `cooldown_minutes` | Refresh que retenta em loop virando metralhadora de cancelamentos |
| `max_actions_per_run` | Muitos itens anômalos de uma vez é sintoma sistêmico, não culpa deles — acima do orçamento, o ciclo só alerta |
| `min_capacity_utilization_percent` | Matar carga numa capacidade que está a 20% de uso |
| `protect` | Regulatório e executivo alertam, nunca são mortos |
| `freeze_windows` | Fechamento contábil e janela de carga noturna |
| `mode: observe` | Tudo acima. É o padrão, e deve continuar sendo por 2–4 semanas |

### Várias capacidades, políticas diferentes

`defaults` define a política; cada capacidade sobrescreve o que precisa. O merge é por bloco,
então mexer em `thresholds.alert` preserva `throttle` e `kill`.

```yaml
defaults:
  mode: observe
  thresholds: { alert: 1.2, throttle: 1.5, kill: 1.8 }

capacities:
  - key: F128_PROD
    id: "..."
    sku: F128
    overrides:
      guards: { consecutive_breaches: 3, max_actions_per_run: 3 }

  - key: F64_SANDBOX
    id: "..."
    sku: F64
    overrides:
      mode: enforce              # o enforcement estreia aqui
      thresholds: { alert: 1.15 }
      freeze_windows: []
```

---

## Calibração

A fase de observação existe para responder a uma pergunta: *com estes limiares, quantas vezes
eu teria matado alguma coisa na semana passada, e o quê?*

```bash
pbi-watchdog calibrate --days 14
```

Faz replay do histórico com a política atual e devolve quantos alertas / throttles / kills
teriam ocorrido, sugestões de threshold a partir da distribuição observada, e os itens que
disparariam ação repetidamente — normalmente cargas legitimamente irregulares que pertencem
a `protect.item_ids`, não abusos.

Só depois disso troque `mode` para `enforce`, e comece pela capacidade menos crítica.

---

## Arquitetura

```
src/pbi_watchdog/
  core/        baseline.py, detect.py   ← funções puras, sem I/O. É onde os testes moram
  config.py    schema pydantic          ← o contrato com o usuário
  auth/        SPN, managed identity, notebook
  sources/     metrics_app_rest | metrics_app_sempy | fake  + perfis de DAX
  storage/     sqlite | delta
  actions/     cancel_refresh, cancel_fabric_jobs, kill_xmla_sessions
  notify/      teams, slack, webhook, console
  runner.py    orquestração
  doctor.py    diagnóstico pré-voo
  calibrate.py replay do histórico
  cli.py
```

Cada camada é um protocolo. Trocar SQLite por outro backend é implementar 9 métodos de
`storage.StateStore`; adicionar um canal de notificação é uma classe com um método `send`.

### Perfis de DAX

O modelo do Capacity Metrics App muda de nome entre versões — é a causa número um de
"funcionou na minha tenant e quebrou na sua". Em vez de embutir um DAX fixo, a lib declara
perfis conhecidos e, no modo REST, testa cada consulta diretamente na primeira capacidade.
Isso evita `INFO.TABLES()` / `INFO.COLUMNS()`, que não são aceitos por `executeQueries`.

```bash
pbi-watchdog inspect-model -v     # testa a leitura e mostra o perfil que funcionou
```

Se nenhum bater, `metrics_source.dax_override` aceita a sua query. Ela só precisa devolver
`item_id, item_name, item_kind, workspace_id, workspace_name, cu_seconds_today`.

---

## Uso como biblioteca

```python
from pbi_watchdog import WatchdogConfig, Watchdog

config = WatchdogConfig.from_file("watchdog.yaml")
for summary in Watchdog(config, dry_run=True).run_once():
    print(summary.capacity_key, summary.anomalies, summary.actions_taken)
    for event in summary.events:
        print(event.item_name, event.tier, "→", event.effective_tier, event.suppressions)
```

O núcleo também é usável isolado, sem config nem storage:

```python
from pbi_watchdog.core import baseline, detect

intervalos = baseline.derive_intervals(snapshots_atuais, snapshots_anteriores)
baselines  = baseline.compute_baselines(historico, cfg, target_bucket="h14", ...)
veredito   = detect.assess_one(intervalo, baselines["item-x"], policy, estado, now=agora)
```

---

## O que você precisa provisionar

Resumo; o detalhe com passo a passo está em [docs/PERMISSIONS.md](docs/PERMISSIONS.md), e o
`doctor` verifica cada item.

| Item | Para quê | Obrigatório? |
|---|---|---|
| Capacity Metrics App instalado | fonte das métricas | sim |
| Service principal (app registration) | autenticação | sim (fora do Fabric) |
| SPN como Viewer no workspace do Metrics App | ler consumo | sim |
| Tenant setting: *Service principals can use Fabric APIs* | tudo | sim |
| Tenant setting: *Dataset Execute Queries REST API* | fonte `metrics_app_rest` | sim |
| SPN em grupo de *read-only admin APIs* + `Tenant.Read.All` | validar GUIDs, `discover` | recomendado |
| SPN como **Member/Admin** dos workspaces monitorados | cancelar refresh e jobs | só para `enforce` |
| XMLA read-write na capacidade | `kill_xmla_sessions` | só para tier `kill` |
| Volume persistente para `storage.path` | manter a baseline | sim |

**Só alertar não exige permissão de escrita em lugar nenhum.** Se a organização não quiser
dar poder de cancelamento ao watchdog, `mode: observe` entrega valor sem isso.

---

## Limitações conhecidas

- O Capacity Metrics App tem **latência de alguns minutos**. A contenção nunca é instantânea —
  o watchdog reduz o rabo do incidente, não o previne.
- A granularidade é o intervalo entre execuções. Um pico de 3 minutos entre dois snapshots
  de 15 minutos aparece diluído.
- `kill_xmla_sessions` derruba usuários no meio do relatório e exige `sempy`/ADOMD, ou seja,
  só roda dentro do Fabric. Fora dele a ação falha explicitamente e o alerta continua saindo.
- `cancel_refresh` só enxerga refreshes com status `Unknown` (o indicador de "em andamento"
  na API do Power BI).
- Perder o arquivo de `storage` significa perder a baseline: o watchdog volta a só observar
  até reacumular histórico. Monte em volume persistente.
- Rodar o watchdog **na** capacidade monitorada faz dele parte do consumo que ele mede. É leve,
  mas prefira uma capacidade diferente ou um runtime externo.

---

## Desenvolvimento

```bash
pip install -e ".[dev]"
pytest                       # núcleo, config, CLI, doctor e end-to-end com fonte sintética
ruff check src tests
```

Os testes não tocam a rede: a fonte sintética e o SQLite temporário cobrem o ciclo completo,
incluindo bootstrap, streak, cooldown, circuit breaker, gap do agendador e falha de ação.

## Publicação

O pacote é um wheel `py3-none-any` padrão, sem extensão compilada.

```bash
python -m build              # gera dist/*.whl e dist/*.tar.gz
twine check dist/*
```

Publicar é criar uma tag — o workflow [release.yml](.github/workflows/release.yml) usa
Trusted Publishing (OIDC), sem token guardado no repositório:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

Antes da primeira publicação, registre o trusted publisher em
<https://pypi.org/manage/account/publishing/> com workflow `release.yml` e environment `pypi`.

Para validar o fluxo inteiro sem queimar a versão no PyPI (versões publicadas são imutáveis
e o nome não é liberável), publique antes no TestPyPI:

```bash
twine upload --repository testpypi dist/*
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ pbi-watchdog
```
