"""Daily Content's deployment artifacts — checked as files, never applied.

``docker-compose.prod.yml``, the edge's server template, the preflight
script, ``.env.example``, the backend image and the runbook must agree with
one another and with the code: one webhook path, the same ports, a connection
budget PostgreSQL can hold, and a profile that leaves the shop's own
deployment exactly as it was. The restore script must stop Daily Content
along with everything else that writes to the database.

Nothing here talks to a server or starts a container: the restore script
runs against a stand-in for the docker CLI. The repository root is
needed (these files live outside ``backend/``); without it the module skips.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from app.bot.webhooks import HEALTH_PATH
from app.core.config import BACKEND_DIR, DailySettings, PostgresSettings
from app.daily_worker.runner import SHUTDOWN_GRACE_SECONDS

if TYPE_CHECKING:
    from pathlib import Path

REPO = BACKEND_DIR.parent
COMPOSE_FILE = REPO / "docker-compose.prod.yml"
SERVER_TEMPLATE = REPO / "nginx" / "templates" / "tgshop.conf.template"
PREFLIGHT = REPO / "scripts" / "preflight.sh"
RESTORE = REPO / "scripts" / "restore.sh"
ENV_EXAMPLE = REPO / ".env.example"
RUNBOOK = REPO / "docs" / "daily-content.md"

pytestmark = pytest.mark.skipif(
    not COMPOSE_FILE.exists(),
    reason="the repository root (deployment files) is not available here",
)

DAILY_SERVICES = ("daily-bot", "daily-worker")
SHOP_SERVICES = ("postgres", "redis", "migrations", "api", "bot", "nginx")
SUPERUSER_RESERVED = 3
HEADROOM = 5
"""A migration run and an operator's psql session, on top of every pool's peak."""


def _compose() -> dict[str, Any]:
    document = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _service(name: str) -> dict[str, Any]:
    service = _compose()["services"][name]
    assert isinstance(service, dict)
    return service


def _seconds(duration: str) -> int:
    match = re.fullmatch(r"(\d+)s", duration)
    assert match, duration
    return int(match.group(1))


def _template() -> str:
    return SERVER_TEMPLATE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# docker-compose.prod.yml                                                     #
# --------------------------------------------------------------------------- #


def test_daily_content_sits_behind_its_own_profile() -> None:
    services = _compose()["services"]

    for name in SHOP_SERVICES:
        assert "profiles" not in services[name], f"{name} must start with a plain `up`"
    for name in DAILY_SERVICES:
        assert services[name]["profiles"] == ["daily"]


@pytest.mark.parametrize("name", DAILY_SERVICES)
def test_the_daily_containers_run_like_the_rest_of_the_stack(name: str) -> None:
    service = _service(name)
    bot = _service("bot")

    assert service["image"] == bot["image"] == "tgshop-backend:latest"
    assert service["restart"] == "always"
    assert _seconds(service["stop_grace_period"]) == 30
    assert service["deploy"]["replicas"] == 1
    assert service["logging"] == bot["logging"]
    assert service["logging"]["options"] == {"max-size": "10m", "max-file": "5"}
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["env_file"] == [".env"]
    assert service["depends_on"] == {
        "postgres": {"condition": "service_healthy"},
        "redis": {"condition": "service_healthy"},
        "migrations": {"condition": "service_completed_successfully"},
    }
    assert set(service["networks"]) == {"data", "edge"}
    assert service["environment"]["POSTGRES_HOST"] == "postgres"
    assert service["environment"]["REDIS_HOST"] == "redis"
    assert "ports" not in service  # reachable through nginx only, or not at all


def test_the_daily_bot_and_the_edge_agree_on_the_webhook() -> None:
    daily = DailySettings()
    service = _service("daily-bot")
    environment = service["environment"]

    assert service["command"] == ["python", "-m", "app.daily_bot"]
    assert environment["DAILY_USE_WEBHOOK"] == "true"
    assert environment["DAILY_WEBHOOK_PATH"] == daily.webhook_path == "/webhook/daily"
    assert int(environment["DAILY_BOT_PORT"]) == daily.bot_port == 8082
    assert f"127.0.0.1:8082{HEALTH_PATH}" in service["healthcheck"]["test"][-1]
    template = _template()
    assert "location = /webhook/daily {" in template
    assert "set $tgshop_daily_bot http://daily-bot:8082;" in template


def test_the_worker_serves_its_health_check_on_8083() -> None:
    daily = DailySettings()
    service = _service("daily-worker")

    assert service["command"] == ["python", "-m", "app.daily_worker"]
    assert int(service["environment"]["DAILY_WORKER_PORT"]) == daily.worker_port == 8083
    assert f"127.0.0.1:8083{HEALTH_PATH}" in service["healthcheck"]["test"][-1]


def test_a_deploy_lets_the_worker_finish_the_pass_it_is_in() -> None:
    grace = _seconds(_service("daily-worker")["stop_grace_period"])

    assert grace > SHUTDOWN_GRACE_SECONDS


def _peak_connections(name: str, service: dict[str, Any]) -> int:
    defaults = PostgresSettings.model_fields
    environment = service.get("environment", {})
    pool = int(environment.get("POSTGRES_POOL_SIZE", defaults["pool_size"].default))
    overflow = int(environment.get("POSTGRES_MAX_OVERFLOW", defaults["max_overflow"].default))
    workers = 1
    if name == "api":
        command = service["command"]
        flag = command.index("--workers")
        match = re.fullmatch(r"\$\{API_WORKERS:-(\d+)\}", command[flag + 1])
        assert match, command[flag + 1]
        workers = int(match.group(1))
    return workers * (pool + overflow)


def test_every_pool_at_its_peak_fits_postgres() -> None:
    services = _compose()["services"]
    postgres_command = services["postgres"]["command"]
    max_connections = int(
        next(item for item in postgres_command if str(item).startswith("max_connections=")).split(
            "="
        )[1]
    )
    connected = {"api", "bot", *DAILY_SERVICES}

    peaks = {name: _peak_connections(name, services[name]) for name in sorted(connected)}

    assert peaks == {"api": 40, "bot": 20, "daily-bot": 10, "daily-worker": 10}
    assert sum(peaks.values()) + HEADROOM <= max_connections - SUPERUSER_RESERVED


def test_the_edge_never_waits_for_daily_content() -> None:
    nginx = _service("nginx")

    assert "daily-bot" not in nginx["depends_on"]
    assert "daily-worker" not in nginx["depends_on"]
    template = _template()
    # No upstream block names it: nginx would refuse to start without it.
    upstreams = re.findall(r"upstream\s+\w+\s*\{([^}]*)\}", template)
    assert all("daily-bot" not in block for block in upstreams)
    # Resolved per request through Docker's DNS instead.
    daily = _location(template, "= /webhook/daily")
    assert "resolver         127.0.0.11 valid=10s ipv6=off;" in daily
    assert "proxy_pass $tgshop_daily_bot;" in daily
    assert "limit_except POST" in daily


# --------------------------------------------------------------------------- #
# The edge's server template                                                  #
# --------------------------------------------------------------------------- #


def _location(template: str, selector: str) -> str:
    """The body of ``location <selector> { … }`` (blocks inside it included)."""
    start = template.index(f"location {selector} {{")
    depth = 0
    for index in range(start, len(template)):
        if template[index] == "{":
            depth += 1
        elif template[index] == "}":
            depth -= 1
            if depth == 0:
                return template[start : index + 1]
    message = f"unterminated location {selector}"
    raise AssertionError(message)


def _servers(template: str) -> list[str]:
    servers, start = [], 0
    while (start := template.find("server {", start)) != -1:
        depth = 0
        for index in range(start, len(template)):
            if template[index] == "{":
                depth += 1
            elif template[index] == "}":
                depth -= 1
                if depth == 0:
                    servers.append(template[start : index + 1])
                    start = index + 1
                    break
    return servers


def test_the_server_template_is_well_formed() -> None:
    template = _template()
    code = "\n".join(line.split("#", 1)[0] for line in template.splitlines())

    assert code.count("{") == code.count("}")
    servers = _servers(code)
    assert len(servers) == 4
    (deployment,) = [
        server
        for server in servers
        if "server_name ${TGSHOP_DOMAIN};" in server and "listen      443 ssl;" in server
    ]
    assert "location = /webhook/daily {" in deployment
    for server in servers:
        if server is not deployment:
            assert "/webhook/daily" not in server


def test_the_shops_routes_are_untouched() -> None:
    template = _template()

    assert "server api:8000 max_fails=3 fail_timeout=10s;" in template
    assert "server bot:8081 max_fails=3 fail_timeout=10s;" in template
    webhooks = _location(template, "^~ /webhook/")
    assert "proxy_pass http://tgshop_bot;" in webhooks
    assert "proxy_pass http://tgshop_api;" in _location(template, "= /api/v1/auth/login")
    assert "proxy_pass http://tgshop_api;" in _location(template, "^~ /api/")
    (deployment,) = [server for server in _servers(template) if "listen      443 ssl;" in server]
    assert "try_files $uri $uri/ /index.html;" in _location(deployment, "/")
    # Plain HTTP still only answers the ACME challenge and redirects.
    assert "return 301 https://$host$request_uri;" in _location(template, "/")


# --------------------------------------------------------------------------- #
# .env.example, the image, the runbook                                        #
# --------------------------------------------------------------------------- #

DEPLOYMENT_VARIABLES = (
    "DAILY_ENABLED",
    "DAILY_BETA_USER_IDS",
    "DAILY_ADMIN_TELEGRAM_IDS",
    "DAILY_BOT_TOKEN",
    "DAILY_BOT_USERNAME",
    "DAILY_WEBHOOK_BASE_URL",
    "DAILY_WEBHOOK_SECRET",
)


def _example_values() -> dict[str, str]:
    values = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.startswith("DAILY_") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def test_env_example_documents_every_daily_variable_the_deployment_needs() -> None:
    values = _example_values()
    text = ENV_EXAMPLE.read_text(encoding="utf-8")

    for key in DEPLOYMENT_VARIABLES:
        assert key in values, key
    assert values["DAILY_ENABLED"] == "false"
    assert values["DAILY_BOT_TOKEN"] == ""
    assert values["DAILY_WEBHOOK_SECRET"] == ""
    assert "# COMPOSE_PROFILES=daily" in text
    for knob in (
        "DAILY_HEARTBEAT_INTERVAL_SECONDS",
        "DAILY_WATCHDOG_INTERVAL_SECONDS",
        "DAILY_WATCHDOG_STALE_SECONDS",
        "DAILY_WATCHDOG_COOLDOWN_MINUTES",
    ):
        assert f"# {knob}=" in text, knob


def test_the_template_as_it_is_never_stops_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every process loads these settings: an empty line copied as is must load."""
    for key in list(os.environ):
        if key.startswith("DAILY_"):
            monkeypatch.delenv(key)
    for key, value in _example_values().items():
        monkeypatch.setenv(key, value)

    daily = DailySettings()

    assert daily.enabled is False
    assert daily.bot_token is None
    assert daily.webhook_secret is None
    assert daily.webhook_base_url is None
    assert daily.admin_telegram_ids == ()
    assert "DAILY_BOT_TOKEN is not set" in daily.bot_problems()


def test_the_image_refuses_to_build_without_the_tz_database() -> None:
    dockerfile = (BACKEND_DIR / "Dockerfile").read_text(encoding="utf-8")
    runtime = dockerfile[dockerfile.index("AS runtime") :]

    assert "RUN python -c \"from zoneinfo import ZoneInfo; ZoneInfo('Europe/Moscow')\"" in runtime


def test_the_runbook_covers_the_launch() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    for key in (*DEPLOYMENT_VARIABLES, "COMPOSE_PROFILES", "DAILY_WATCHDOG_STALE_SECONDS"):
        assert key in runbook, key
    for section in ("BotFather", "Webhook", "Бета", "Выключение флага", "Откат", "Watchdog"):
        assert section in runbook, section
    assert "docs/daily-content.md" in (REPO / "README.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# scripts/preflight.sh                                                        #
# --------------------------------------------------------------------------- #

SHOP_ENV = {
    "APP_ENVIRONMENT": "production",
    "APP_DEBUG": "false",
    "APP_LOG_FORMAT": "json",
    "APP_API_PREFIX": "/api/v1",
    "TGSHOP_DOMAIN": "shop.example.test",
    "TGSHOP_ACME_EMAIL": "ops@example.test",
    "TELEGRAM_USE_WEBHOOK": "true",
    "TELEGRAM_BOT_TOKEN": "123456789:AAHshop-token-for-the-preflight-test-1",
    "TELEGRAM_BOT_USERNAME": "ExampleShopBot",
    "TELEGRAM_WEBHOOK_SECRET": "shop-webhook-secret-0123456789",
    "TELEGRAM_WEBHOOK_BASE_URL": "https://shop.example.test",
    "TELEGRAM_WEBHOOK_PATH": "/webhook/telegram",
    "CRYPTOBOT_WEBHOOK_PATH": "/webhook/cryptobot",
    "CRYPTOBOT_API_TOKEN": "1234:cryptobot-token",
    "CRYPTOBOT_NETWORK": "mainnet",
    "POSTGRES_PASSWORD": "postgres-password-0123456789",
    "REDIS_PASSWORD": "redis-password-0123456789",
    "REFERRAL_ENABLED": "false",
    "SECURITY_JWT_SECRET": "j" * 48,
    "SECURITY_ADMIN_PASSWORD": "admin-password-0123",
    "SECURITY_COOKIE_SECURE": "true",
    "SECURITY_CORS_ORIGINS": "",
}

DAILY_ENV = {
    "COMPOSE_PROFILES": "daily",
    "DAILY_ENABLED": "false",
    "DAILY_ADMIN_TELEGRAM_IDS": "111",
    "DAILY_BOT_TOKEN": "987654321:AAHdaily-token-for-the-preflight-te",
    "DAILY_BOT_USERNAME": "ExampleDailyBot",
    "DAILY_WEBHOOK_BASE_URL": "https://shop.example.test",
    "DAILY_WEBHOOK_SECRET": "daily-webhook-secret-0123456789",
}


def _preflight(tmp_path: Path, **values: str) -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / "production.env"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    env_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return subprocess.run(  # noqa: S603 — a fixed script, a temporary env file
        ["bash", str(PREFLIGHT)],  # noqa: S607
        env={**os.environ, "ENV_FILE": str(env_file)},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_preflight_without_daily_content_changes_nothing(tmp_path: Path) -> None:
    result = _preflight(tmp_path, **SHOP_ENV)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Daily Content is not deployed" in result.stdout


def test_preflight_accepts_a_deployed_idle_daily_content(tmp_path: Path) -> None:
    result = _preflight(tmp_path, **SHOP_ENV, **DAILY_ENV)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "COMPOSE_PROFILES includes 'daily'" in result.stdout
    assert "DAILY_WEBHOOK_BASE_URL matches TGSHOP_DOMAIN" in result.stdout
    assert "deployed and idle" in result.stdout


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        (
            {"DAILY_BOT_TOKEN": SHOP_ENV["TELEGRAM_BOT_TOKEN"]},
            "DAILY_BOT_TOKEN must not be the shop bot's TELEGRAM_BOT_TOKEN",
        ),
        ({"DAILY_BOT_USERNAME": "exampleshopbot"}, "must not be the shop bot's"),
        ({"DAILY_WEBHOOK_SECRET": SHOP_ENV["TELEGRAM_WEBHOOK_SECRET"]}, "must differ"),
        ({"DAILY_WEBHOOK_BASE_URL": "https://elsewhere.test"}, "DAILY_WEBHOOK_BASE_URL must be"),
        ({"DAILY_BOT_TOKEN": ""}, "DAILY_BOT_TOKEN is empty"),
        ({"COMPOSE_PROFILES": "", "DAILY_ENABLED": "true"}, "would not run"),
    ],
    ids=["shop-token", "shop-username", "shop-secret", "other-host", "no-token", "no-profile"],
)
def test_preflight_refuses_a_daily_content_that_would_break(
    tmp_path: Path,
    overrides: dict[str, str],
    error: str,
) -> None:
    result = _preflight(tmp_path, **{**SHOP_ENV, **DAILY_ENV, **overrides})

    assert result.returncode == 1, result.stdout
    assert error in result.stdout


def test_preflight_warns_about_a_launch_without_the_shops_button(tmp_path: Path) -> None:
    result = _preflight(
        tmp_path,
        **SHOP_ENV,
        **{**DAILY_ENV, "DAILY_ENABLED": "true", "DAILY_BETA_USER_IDS": "111,222"},
        LOYALTY_ENABLED="false",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "a beta: only those buyers are served" in result.stdout
    assert "LOYALTY_ENABLED is not true" in result.stdout


# --------------------------------------------------------------------------- #
# scripts/restore.sh                                                          #
# --------------------------------------------------------------------------- #

# A stand-in for the docker CLI: it records every call, answers the two
# questions the script asks (`compose version`, `ps`), swallows the dump it is
# handed, and fails where a test says so. Nothing reaches a Docker daemon.
FAKE_DOCKER = r"""#!/usr/bin/env bash
call="${*//$'\n'/ }"
case "${call}" in
    *"exec -T postgres sh -c"*) cat > /dev/null; exit 0 ;;
esac
printf '%s\n' "${call}" >> "${FAKE_DOCKER_LOG}"
case "${call}" in
    *" ps "*) printf '%s\n' ${FAKE_RUNNING} ;;
    *"exec -T postgres pg_restore"*) cat > /dev/null; exit "${FAKE_RESTORE_EXIT:-0}" ;;
    *"run --rm migrations"*) exit "${FAKE_MIGRATE_EXIT:-0}" ;;
esac
exit 0
"""

WITH_DAILY = "postgres redis migrations api bot daily-bot daily-worker nginx"
WITHOUT_DAILY = "postgres redis migrations api bot nginx"

STOP_EVERY_WRITER = "--profile daily stop api bot daily-bot daily-worker"
RESTORE_THE_DUMP = (
    "exec -T postgres pg_restore --username shop --dbname shop --clean --if-exists "
    "--no-owner --no-privileges --single-transaction --exit-on-error"
)
MIGRATE = "run --rm migrations alembic upgrade head"
START_EVERY_WRITER = "--profile daily up -d api bot daily-bot daily-worker"
RELOAD_THE_EDGE = "exec -T nginx nginx -s reload"


def _restore(
    tmp_path: Path,
    *,
    running: str,
    **failures: str,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """``restore.sh --yes`` with ``running`` services; its result and what it changed.

    The changes are the docker calls that are not questions (``version``,
    ``ps``), in order, without the ``compose --env-file … -f …`` prefix.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(stat.S_IRWXU)
    env_file = tmp_path / "production.env"
    env_file.write_text("POSTGRES_USER=shop\nPOSTGRES_DB=shop\nTGSHOP_DOMAIN=shop.example.test\n")
    dump = tmp_path / "tgshop.dump"
    dump.write_bytes(b"PGDMP stand-in archive")
    calls_log = tmp_path / "docker-calls.log"
    result = subprocess.run(  # noqa: S603 — a fixed script, a stand-in docker
        ["bash", str(RESTORE), str(dump), "--yes"],  # noqa: S607
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "ENV_FILE": str(env_file),
            "FAKE_DOCKER_LOG": str(calls_log),
            "FAKE_RUNNING": running,
            **failures,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    calls = calls_log.read_text().splitlines() if calls_log.exists() else []
    changes = [re.sub(r"^compose --env-file \S+ -f \S+ ", "", call) for call in calls]
    return result, [
        change for change in changes if change != "compose version" and " ps " not in f" {change}"
    ]


@pytest.mark.parametrize(
    ("running", "started"),
    [
        (WITH_DAILY, START_EVERY_WRITER),
        (WITHOUT_DAILY, "--profile daily up -d api bot"),
        (
            WITHOUT_DAILY.replace("bot", "bot daily-worker"),
            "--profile daily up -d api bot daily-worker",
        ),
    ],
    ids=["with-daily", "without-daily", "worker-only"],
)
def test_restore_stops_every_writer_first_and_starts_again_what_ran(
    tmp_path: Path,
    running: str,
    started: str,
) -> None:
    result, changes = _restore(tmp_path, running=running)

    assert result.returncode == 0, result.stdout + result.stderr
    assert changes == [STOP_EVERY_WRITER, RESTORE_THE_DUMP, MIGRATE, started, RELOAD_THE_EDGE]


def test_a_failed_restore_starts_the_writers_again_on_the_old_data(tmp_path: Path) -> None:
    result, changes = _restore(tmp_path, running=WITH_DAILY, FAKE_RESTORE_EXIT="1")

    assert result.returncode == 1
    assert "restore failed." in result.stderr
    assert changes == [STOP_EVERY_WRITER, RESTORE_THE_DUMP, START_EVERY_WRITER, RELOAD_THE_EDGE]


def test_the_writers_stay_stopped_while_the_restored_schema_cannot_migrate(
    tmp_path: Path,
) -> None:
    result, changes = _restore(tmp_path, running=WITH_DAILY, FAKE_MIGRATE_EXIT="1")

    assert result.returncode == 1
    assert "api bot daily-bot daily-worker stay stopped" in result.stderr
    assert changes == [STOP_EVERY_WRITER, RESTORE_THE_DUMP, MIGRATE]


def test_restore_leaves_an_edge_that_is_not_running_alone(tmp_path: Path) -> None:
    result, changes = _restore(tmp_path, running=WITH_DAILY.replace(" nginx", ""))

    assert result.returncode == 0, result.stdout + result.stderr
    assert changes == [STOP_EVERY_WRITER, RESTORE_THE_DUMP, MIGRATE, START_EVERY_WRITER]
