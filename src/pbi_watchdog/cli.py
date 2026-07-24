"""Interface de linha de comando.

    pbi-watchdog init            gera watchdog.yaml comentado
    pbi-watchdog doctor          diz exatamente o que falta para funcionar
    pbi-watchdog discover        lista capacidades e o dataset do Metrics App
    pbi-watchdog inspect-model   mostra tabelas/colunas do Metrics App
    pbi-watchdog run             executa um ciclo (--dry-run, --loop)
    pbi-watchdog calibrate       sugere thresholds a partir do histórico
    pbi-watchdog report          resume os eventos recentes
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import calibrate as calib
from . import clock
from . import doctor as doc
from .config import WatchdogConfig
from .rest import PBI_BASE, ApiError, RestClient
from .storage import build_store

TEMPLATE = Path(__file__).parent / "templates" / "watchdog.example.yaml"


def _load(args) -> WatchdogConfig:
    try:
        return WatchdogConfig.from_file(args.config)
    except Exception as e:
        # Erro de config precisa sair legível, não como traceback: quem está preenchendo o
        # YAML não deve precisar ler o stack do pydantic.
        print(f"❌ Config inválida ({args.config}):\n{e}", file=sys.stderr)
        raise SystemExit(2) from None


# --------------------------------------------------------------------------- comandos


def cmd_init(args) -> int:
    dest = Path(args.output)
    if dest.exists() and not args.force:
        print(f"❌ {dest} já existe. Use --force para sobrescrever.", file=sys.stderr)
        return 1
    shutil.copy(TEMPLATE, dest)
    print(f"✅ Template criado em {dest}\n")
    print("Próximos passos:")
    print("  1. Preencha tenant_id / client_id e exporte PBI_CLIENT_SECRET no ambiente")
    print("  2. pbi-watchdog discover --capacities   (para pegar os GUIDs)")
    print("  3. pbi-watchdog discover --metrics      (para achar o dataset do Metrics App)")
    print("  4. pbi-watchdog doctor --deep           (valida tudo de ponta a ponta)")
    print("  5. pbi-watchdog run --dry-run           (primeiro ciclo, sem agir)")
    return 0


def cmd_doctor(args) -> int:
    config = _load(args)
    checks = doc.Doctor(config).run(deep=args.deep)
    print(doc.render(checks))
    return 1 if any(c.status == doc.FAIL for c in checks) else 0


def cmd_discover(args) -> int:
    config = _load(args)
    from .auth import build_token_provider

    client = RestClient(build_token_provider(config.auth))
    rc = 0

    if args.capacities or not (args.capacities or args.metrics):
        print("== Capacidades visíveis ao principal ==")
        try:
            data = client.get(f"{PBI_BASE}/admin/capacities")
            rows = data.get("value", [])
            if not rows:
                print("  (nenhuma — verifique as permissões de admin API)")
            for c in rows:
                print(
                    f"  {c.get('displayName','?'):<40} sku={c.get('sku','?'):<8} "
                    f"state={c.get('state','?')}"
                )
                print(f"      id: {c.get('id')}")
            print("\n  Cole os ids em `capacities[].id` na config.\n")
        except ApiError as e:
            print(f"  ❌ HTTP {e.status}: precisa de Tenant.Read.All + service principal habilitado "
                  f"para admin APIs read-only.\n")
            rc = 1

    if args.metrics or not (args.capacities or args.metrics):
        print("== Candidatos a Capacity Metrics App ==")
        try:
            groups = client.get(f"{PBI_BASE}/groups", params={"$top": 200})
            found = False
            for g in groups.get("value", []):
                name = g.get("name", "")
                if "metric" not in name.lower() and "capacity" not in name.lower():
                    continue
                datasets = client.get(f"{PBI_BASE}/groups/{g['id']}/datasets")
                for d in datasets.get("value", []):
                    found = True
                    print(f"  workspace: {name}")
                    print(f"    workspace_id: {g['id']}")
                    print(f"    dataset: {d.get('name')}")
                    print(f"    dataset_id:   {d.get('id')}")
            if not found:
                print("  (nada encontrado — adicione o SPN como Viewer no workspace do Metrics App)")
        except ApiError as e:
            print(f"  ❌ HTTP {e.status} ao listar workspaces.")
            rc = 1
    return rc


def cmd_inspect_model(args) -> int:
    config = _load(args)
    from .auth import build_token_provider
    from .sources import MetricsAppRestSource
    from .sources.profiles import join_tables_and_columns, table_names

    source = MetricsAppRestSource(
        config.metrics_source, RestClient(build_token_provider(config.auth))
    )
    raw_tables, raw_columns = source._introspect()
    tables = table_names(raw_tables)
    columns = join_tables_and_columns(raw_tables, raw_columns)
    print(f"== Tabelas ({len(tables)}) ==")
    for t in sorted(tables):
        print(f"  {t}")
    if args.verbose:
        print(f"\n== Colunas ({len(columns)}) ==")
        for c in sorted(columns):
            print(f"  {c}")
    from .sources.profiles import PROBE_ORDER, PROFILES, missing_requirements

    print("\n== Compatibilidade de perfis ==")
    for name in PROBE_ORDER:
        missing = missing_requirements(PROFILES[name], tables, columns)
        detalhe = f" — falta: {', '.join(missing)}" if missing else ""
        print(f"  {'❌' if missing else '✅'} {name}{detalhe}")
    return 0


def cmd_run(args) -> int:
    config = _load(args)
    from .runner import Watchdog

    wd = Watchdog(config, dry_run=args.dry_run)
    try:
        while True:
            summaries = wd.run_once(capacity_keys=args.capacity or None)
            for s in summaries:
                status = "ERRO" if s.errors else "ok"
                print(
                    f"[{s.started_at:%Y-%m-%d %H:%M}] {s.capacity_key} ({s.mode}): "
                    f"{s.items_scanned} itens, {s.anomalies} anomalias, "
                    f"{s.actions_taken} ações — {status}"
                )
                for e in s.errors:
                    print(f"    ! {e}")
                if args.verbose:
                    for ev in s.events:
                        print(
                            f"    {ev.tier}->{ev.effective_tier} {ev.item_name} "
                            f"ratio={ev.ratio:.2f} cu={ev.cu_seconds:,.0f} "
                            f"[{ev.suppressions or 'sem supressão'}] {ev.actions}"
                        )
            if not args.loop:
                break
            interval = min(c.policy.interval_minutes for c in config.enabled_capacities if c.policy)
            time.sleep(interval * 60)
        return 0
    finally:
        wd.close()


def cmd_calibrate(args) -> int:
    config = _load(args)
    reports = calib.calibrate_all(config, days=args.days, capacity_keys=args.capacity or None)
    for r in reports:
        print(calib.render(r, top=args.top))
        print()
    return 0


def cmd_report(args) -> int:
    config = _load(args)
    store = build_store(config.storage)
    try:
        since = clock.now_in(config.timezone) - dt.timedelta(days=args.days)
        events = store.load_events(since, capacity_key=args.capacity_key)
    finally:
        store.close()

    if args.json:
        print(json.dumps([e.__dict__ for e in events], default=str, indent=2))
        return 0

    if not events:
        print(f"Nenhum evento nos últimos {args.days} dia(s).")
        return 0

    print(f"== {len(events)} evento(s) nos últimos {args.days} dia(s) ==\n")
    print(f"{'quando':<17}{'capacidade':<14}{'tier':<10}{'aplicado':<10}{'ratio':>7}  item")
    for e in events[: args.limit]:
        print(
            f"{e.ts:%d/%m %H:%M}    {e.capacity_key[:13]:<14}{e.tier:<10}{e.effective_tier:<10}"
            f"{(e.ratio or 0):>7.2f}  {e.item_name[:40]}"
        )
    by_tier: dict = {}
    for e in events:
        by_tier[e.tier] = by_tier.get(e.tier, 0) + 1
    suppressed = sum(1 for e in events if e.tier != e.effective_tier)
    print(f"\nPor tier detectado: {by_tier}")
    print(f"Contidos por travas de segurança: {suppressed}/{len(events)}")
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    # As flags globais valem antes e depois do subcomando: `run -v` é a forma que as pessoas
    # digitam, e o argparse sozinho só aceitaria `-v run`.
    #
    # Os dois lados usam SUPPRESS para que o subparser não sobrescreva, com seu default, o
    # que veio antes do subcomando. Os valores efetivos são aplicados em `main` e NÃO via
    # set_defaults: `parents=` compartilha os mesmos objetos Action entre os parsers, e
    # set_defaults muta `action.default`, o que apagaria o SUPPRESS em todos eles.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c", "--config", default=argparse.SUPPRESS,
        help="caminho da config (default: watchdog.yaml)",
    )
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)

    p = argparse.ArgumentParser(
        prog="pbi-watchdog", description=__doc__, parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    s = add("init", help="gera um watchdog.yaml comentado")
    s.add_argument("-o", "--output", default="watchdog.yaml")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = add("doctor", help="valida config, auth, acesso e prontidão")
    s.add_argument("--deep", action="store_true", help="inclui um snapshot real e checagem de escrita")
    s.set_defaults(func=cmd_doctor)

    s = add("discover", help="lista capacidades e o dataset do Metrics App")
    s.add_argument("--capacities", action="store_true")
    s.add_argument("--metrics", action="store_true")
    s.set_defaults(func=cmd_discover)

    s = add("inspect-model", help="tabelas/colunas do Metrics App e perfis compatíveis")
    s.set_defaults(func=cmd_inspect_model)

    s = add("run", help="executa um ciclo")
    s.add_argument("--dry-run", action="store_true", help="avalia e alerta, mas não executa ações")
    s.add_argument("--loop", action="store_true", help="repete no intervalo configurado")
    s.add_argument("--capacity", action="append", help="restringe a estas capacidades (repetível)")
    s.set_defaults(func=cmd_run)

    s = add("calibrate", help="sugere thresholds a partir do histórico")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--top", type=int, default=15)
    s.add_argument("--capacity", action="append")
    s.set_defaults(func=cmd_calibrate)

    s = add("report", help="resume eventos recentes")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--capacity-key")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_report)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # Com SUPPRESS, o atributo só existe se a flag foi passada — em qualquer das posições.
    args.config = getattr(args, "config", "watchdog.yaml")
    args.verbose = getattr(args, "verbose", False)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
