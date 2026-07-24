# Provisionamento — o que precisa existir antes do primeiro ciclo

Este documento é a lista completa. Cada item aparece como uma checagem em
`pbi-watchdog doctor --deep`, então rode-o depois de cada passo em vez de fazer tudo e
descobrir no fim o que faltou.

A regra que organiza tudo abaixo: **alertar não exige nenhuma permissão de escrita.**
As permissões caras só entram quando você quer que o watchdog *aja*.

---

## 1. Capacity Metrics App

Instale o **Microsoft Fabric Capacity Metrics** pelo AppSource, com um usuário que seja
admin das capacidades. Ele cria um workspace com um semantic model, e é dele que sai todo
o consumo de CU.

Anote o workspace e o dataset — ou deixe o `discover` achar:

```bash
pbi-watchdog discover --metrics
```

Sem o app instalado não há o que monitorar; não existe API pública equivalente com
granularidade por item.

---

## 2. Service principal

No Entra ID, crie um **App registration**. Guarde `tenant_id`, `client_id` e um segredo
(ou um certificado, preferível em produção).

Não é preciso adicionar API permissions do tipo delegated. O acesso do Power BI a service
principals é controlado pelas configurações de tenant do passo 3.

Exporte no ambiente onde o watchdog roda:

```bash
export PBI_CLIENT_ID="..."
export PBI_CLIENT_SECRET="..."
```

O YAML referencia com `${PBI_CLIENT_SECRET}` e nunca contém o valor.

---

## 3. Configurações de tenant (Admin Portal → Tenant settings)

| Configuração | Onde | Necessária para |
|---|---|---|
| **Service principals can use Fabric APIs** | Developer settings | tudo |
| **Dataset Execute Queries REST API** | Integration settings | fonte `metrics_app_rest` |
| **Service principals can use read-only admin APIs** | Admin API settings | `discover`, validação de GUIDs |
| **Enhance admin APIs responses with detailed metadata** | Admin API settings | opcional, melhora o `discover` |

Em todas elas, habilite para um **grupo de segurança específico** contendo o SPN — não para
toda a organização.

Mudanças de tenant setting levam alguns minutos para propagar. Se o `doctor` acusar 401
logo após habilitar, espere e repita.

---

## 4. Acesso ao workspace do Metrics App

Adicione o service principal como **Viewer** no workspace do Capacity Metrics App.

Verificação:

```bash
pbi-watchdog doctor          # a checagem "Acesso ao Metrics App" precisa passar
pbi-watchdog inspect-model   # confirma que um perfil de DAX é compatível
```

---

## 5. Permissões de admin (recomendado, não obrigatório)

Para que o watchdog valide os GUIDs configurados e o `discover --capacities` funcione, o SPN
precisa de `Tenant.Read.All` e estar no grupo permitido em *read-only admin APIs*.

Sem isso o watchdog funciona normalmente — apenas não valida se os GUIDs estão certos, o que
transfere para você o custo de um GUID digitado errado (que se manifesta como "nenhum item
retornado", não como erro).

---

## 6. Permissões para AGIR — só se `mode: enforce`

Cancelar refresh ou job exige que o principal tenha poder de escrita **no workspace do item**,
não apenas admin de leitura no tenant.

| Ação | Requisito |
|---|---|
| `cancel_refresh` | SPN como **Member** ou **Admin** do workspace do dataset |
| `cancel_fabric_jobs` | idem, e token do recurso Fabric disponível |
| `kill_xmla_sessions` | **XMLA endpoint = Read Write** na capacidade (Admin Portal → Capacity settings), e runtime com `sempy`/ADOMD — na prática, dentro do Fabric |

`pbi-watchdog doctor --deep` inclui a checagem "Alcance de escrita".

Se a organização não quiser conceder isso, mantenha `mode: observe`. O watchdog continua
detectando e alertando, e alguém decide o que fazer.

---

## 7. Persistência

`storage.path` guarda snapshots, intervalos, eventos e o estado por item. **É a baseline.**
Perdê-lo faz o watchdog voltar a só observar até reacumular `min_days` de histórico.

- Container / Azure Container Apps: monte um Azure Files share
- Kubernetes: PersistentVolumeClaim
- Azure Function: Azure Files montado (o disco local é efêmero)
- Notebook Fabric: use `storage.kind: delta` com Lakehouse anexado

Faça backup do arquivo junto com o resto da sua infraestrutura. Ele é pequeno e reconstruí-lo
custa semanas de observação.

---

## 8. Agendamento

O watchdog é stateless entre execuções (todo o estado está no storage), então qualquer
agendador serve. `interval_minutes` na config precisa bater com a frequência real — é o valor
usado para normalizar o consumo.

```bash
# cron, a cada 15 minutos
*/15 * * * * cd /opt/watchdog && pbi-watchdog run >> /var/log/watchdog.log 2>&1
```

Não rode dois ciclos concorrentes sobre a mesma capacidade: o SQLite serializa a escrita, mas
o intervalo derivado ficaria dividido entre eles.

---

## Ordem sugerida de rollout

1. Passos 1–4. `doctor` verde, `mode: observe` em todas as capacidades, notificação para um
   canal **só seu** — não o do time ainda.
2. Duas a quatro semanas coletando. Nada acontece; é de propósito.
3. `pbi-watchdog calibrate --days 14`. Ajuste thresholds, `min_cu_seconds` e popule `protect`
   com o que o relatório apontar como ruidoso.
4. Notificação para o canal do time, ainda em `observe`. Deixe o time ver os alertas e
   discordar deles por uma semana.
5. Passo 6 e `mode: enforce` **em uma capacidade não crítica**, com `max_actions_per_run`
   baixo (2 ou 3).
6. Produção, semanas depois, se o passo 5 não gerou nenhuma surpresa.

Pular para o passo 5 no primeiro dia é a maneira mais rápida de derrubar carga legítima e
queimar a credibilidade da ferramenta dentro da organização.
