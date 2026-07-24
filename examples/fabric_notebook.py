# ---------------------------------------------------------------------------------------
# pbi-watchdog dentro de um notebook Fabric
#
# Cada bloco separado por "# CELL" vira uma célula. Anexe um Lakehouse ao notebook antes
# de rodar (o storage delta grava nele) e agende a cada 15 minutos.
# ---------------------------------------------------------------------------------------

# CELL 1 — instalação -------------------------------------------------------------------
# %pip é magic do Fabric e reinicia o interpretador Python da sessão: mantenha-o sozinho
# na primeira célula, senão as variáveis definidas antes se perdem.
#
# Para notebook AGENDADO, prefira instalar a lib num Environment (Workspace > Environment >
# Public libraries > pbi-watchdog) e anexá-lo ao notebook: o %pip reinstala a cada execução
# e custa ~30s de capacidade a cada 15 minutos.
#
# NÃO use `pbi-watchdog[fabric]` aqui: o runtime do Fabric já traz o sempy, e reinstalá-lo
# pode conflitar com a versão do runtime.

# %pip install pbi-watchdog


# CELL 2 — configuração -----------------------------------------------------------------
from pbi_watchdog import Watchdog, WatchdogConfig

TEAMS_WEBHOOK_URL = ""  # opcional, mas um watchdog que age sem avisar ninguém é pior que nenhum

config = WatchdogConfig.from_dict(
    {
        "version": 1,
        "timezone": "America/Sao_Paulo",
        # Usa a identidade que executa o notebook. Ela precisa ser capacity admin e,
        # para as ações de cancelamento, Member/Admin dos workspaces monitorados.
        # Atenção: notebook agendado roda com a identidade do DONO do agendamento.
        "auth": {"kind": "notebook"},
        # Dentro do Fabric, ler via sempy dispensa habilitar o executeQueries no tenant.
        "metrics_source": {
            "kind": "metrics_app_sempy",
            "workspace_name": "Microsoft Fabric Capacity Metrics",
            "dataset_name": "Fabric Capacity Metrics",
            "profile": "auto",
        },
        # Grava no Lakehouse anexado. É a baseline: se sumir, o watchdog volta a só observar.
        "storage": {"kind": "delta", "table_prefix": "watchdog_"},
        "defaults": {
            "mode": "observe",  # 2 a 4 semanas antes de pensar em enforce
            "interval_minutes": 15,  # precisa bater com o agendamento
            "baseline": {"lookback_days": 7, "min_days": 4, "method": "median"},
            "thresholds": {"alert": 1.2, "throttle": 1.5, "kill": 1.8},
            "guards": {
                "min_cu_seconds": 300,
                "consecutive_breaches": 2,
                "cooldown_minutes": 60,
                "max_actions_per_run": 5,
                "min_capacity_utilization_percent": 60,
            },
            "protect": {"name_patterns": ["(?i)regulat", "(?i)\\bantt\\b", "(?i)executiv"]},
            "notify": ([{"kind": "teams", "url": TEAMS_WEBHOOK_URL}] if TEAMS_WEBHOOK_URL else []),
        },
        "capacities": [
            {"key": "F128_PROD", "id": "<guid-da-capacidade>", "sku": "F128"},
            {"key": "F64_SANDBOX", "id": "<guid-da-capacidade>", "sku": "F64"},
        ],
    }
)

print("Capacidades configuradas:", [(c.key, c.policy.mode) for c in config.enabled_capacities])


# CELL 3 — diagnóstico (rode uma vez, depois pode pular) ---------------------------------
# Diz exatamente o que falta: acesso ao Metrics App, perfil de DAX compatível, permissões.
from pbi_watchdog.doctor import Doctor, render

print(render(Doctor(config).run(deep=True)))


# CELL 4 — descobrir os GUIDs das capacidades (rode uma vez) -----------------------------
# Se você ainda não tem os GUIDs, o próprio modelo do Metrics App os lista.
import sempy.fabric as fabric  # noqa: E402

display(
    fabric.evaluate_dax(
        dataset="Fabric Capacity Metrics",
        workspace="Microsoft Fabric Capacity Metrics",
        dax_string='EVALUATE SELECTCOLUMNS(Capacities, "id", [capacityId], "nome", [capacityName])',
    )
)


# CELL 5 — o ciclo (esta é a célula que você agenda) -------------------------------------
wd = Watchdog(config)
try:
    for s in wd.run_once():
        print(
            f"[{s.capacity_key}] modo={s.mode} | {s.items_scanned} itens | "
            f"{s.anomalies} anomalias | {s.actions_taken} ações"
        )
        for e in s.errors:
            print("  ! erro:", e)
        for ev in s.events:
            print(
                f"  {ev.tier} → {ev.effective_tier} | {ev.item_name} | "
                f"ratio={ev.ratio:.2f} | cu={ev.cu_seconds:,.0f} | "
                f"contido por: {ev.suppressions or '—'}"
            )
finally:
    wd.close()


# CELL 6 — calibração (depois de 1 a 2 semanas em observe) -------------------------------
# Responde: com estes limiares, quantas vezes eu teria matado algo na janela analisada?
from pbi_watchdog.calibrate import calibrate_all, render as render_calib  # noqa: E402

for report in calibrate_all(config, days=14):
    print(render_calib(report))


# CELL 7 — inspecionar o histórico direto no Lakehouse -----------------------------------
# As tabelas são Delta comuns: dá para construir um relatório Power BI em cima delas.
display(spark.sql("SELECT * FROM watchdog_events ORDER BY ts DESC LIMIT 100"))
# display(spark.sql("SELECT * FROM watchdog_intervals ORDER BY window_end DESC LIMIT 100"))
