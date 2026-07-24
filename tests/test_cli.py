"""A CLI é a superfície que as pessoas tocam primeiro; erros aqui parecem bug do produto."""

from __future__ import annotations

import pytest

from pbi_watchdog.cli import build_parser, main


def parse(argv):
    """Reproduz o que `main` faz com os defaults suprimidos."""
    args = build_parser().parse_args(argv)
    args.config = getattr(args, "config", "watchdog.yaml")
    args.verbose = getattr(args, "verbose", False)
    return args


@pytest.mark.parametrize(
    "argv",
    [
        ["-c", "custom.yaml", "run"],          # antes do subcomando
        ["run", "-c", "custom.yaml"],          # depois do subcomando
        ["-c", "custom.yaml", "run", "-v"],    # misturado
    ],
)
def test_config_e_aceita_antes_e_depois_do_subcomando(argv):
    assert parse(argv).config == "custom.yaml"


def test_default_de_config_quando_nao_informado():
    assert parse(["run"]).config == "watchdog.yaml"


@pytest.mark.parametrize("argv", [["-v", "run"], ["run", "-v"]])
def test_verbose_aceito_nas_duas_posicoes(argv):
    assert parse(argv).verbose is True


def test_verbose_default_falso():
    assert parse(["run"]).verbose is False


def test_subcomando_e_obrigatorio():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_todos_os_subcomandos_resolvem_uma_funcao():
    for cmd in ["init", "doctor", "discover", "inspect-model", "run", "calibrate", "report"]:
        assert callable(parse([cmd]).func)


def test_config_invalida_sai_com_codigo_2(tmp_path, capsys):
    ruim = tmp_path / "ruim.yaml"
    ruim.write_text("version: 1\ncapacities: []\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        main(["-c", str(ruim), "run"])
    assert e.value.code == 2
    assert "Config inválida" in capsys.readouterr().err


def test_init_gera_config_valida(tmp_path, monkeypatch):
    from pbi_watchdog.config import WatchdogConfig

    monkeypatch.setenv("PBI_CLIENT_ID", "x")
    monkeypatch.setenv("PBI_CLIENT_SECRET", "y")
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://example.invalid/h")
    destino = tmp_path / "gerado.yaml"

    assert main(["init", "-o", str(destino)]) == 0
    assert WatchdogConfig.from_file(destino).capacities


def test_init_nao_sobrescreve_sem_force(tmp_path):
    destino = tmp_path / "existente.yaml"
    destino.write_text("nao me apague", encoding="utf-8")

    assert main(["init", "-o", str(destino)]) == 1
    assert destino.read_text(encoding="utf-8") == "nao me apague"
    assert main(["init", "-o", str(destino), "--force"]) == 0
    assert "version: 1" in destino.read_text(encoding="utf-8")
