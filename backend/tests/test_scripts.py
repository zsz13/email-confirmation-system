"""The guards that keep a test run from having an effect outside this machine.

`make test-e2e` sends a real email. These tests prove that it refuses to without the
explicit opt-in, and that nothing `make test-all` runs can reach it. They run `make -n`
and the script's own guard, so neither starts a container or contacts a provider; the
opt-in variable is stripped from the environment first, so a developer who exports it
cannot make this suite send anything either.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
E2E_SCRIPT = REPOSITORY / "scripts" / "e2e-real-email.sh"
RECIPIENT_SCRIPT = REPOSITORY / "scripts" / "e2e-recipient.sh"
OPT_IN = "RUN_REAL_EMAIL_E2E"
RECIPIENT_OVERRIDE = "E2E_RECIPIENT_EMAIL"
TIMEOUT_OVERRIDE = "E2E_DELIVERY_TIMEOUT_SECONDS"

pytestmark = pytest.mark.skipif(
    shutil.which("make") is None or not E2E_SCRIPT.exists(),
    reason="needs make and the repository's scripts",
)


def run(
    *command: str, recipient: str | None = None, extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {OPT_IN, RECIPIENT_OVERRIDE, TIMEOUT_OVERRIDE}
    }
    if recipient is not None:
        environment[RECIPIENT_OVERRIDE] = recipient
    environment.update(extra or {})
    # The commands are the literals below, never anything a test was given.
    return subprocess.run(  # noqa: S603
        command,
        cwd=REPOSITORY,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_the_script_refuses_to_send_a_real_email_without_the_opt_in() -> None:
    result = run("bash", str(E2E_SCRIPT))

    assert result.returncode != 0
    assert f"{OPT_IN}=1" in result.stderr


def test_the_make_target_refuses_on_the_same_condition() -> None:
    result = run("make", "test-e2e")

    assert result.returncode != 0
    assert f"{OPT_IN}=1" in result.stderr


def test_test_all_runs_the_unit_and_integration_layers_and_nothing_external() -> None:
    # -n prints the recipes, including the sub-makes', without running any of them.
    result = run("make", "-n", "test-all")

    assert result.returncode == 0
    assert "pytest" in result.stdout
    assert "integration-test.sh" in result.stdout
    assert "e2e" not in result.stdout


def test_without_an_override_the_recipient_is_a_unique_mailinator_address() -> None:
    first = run("bash", str(RECIPIENT_SCRIPT))
    second = run("bash", str(RECIPIENT_SCRIPT))

    assert first.returncode == 0, first.stderr
    assert re.fullmatch(r"ecs-e2e-\d{8}-\d{6}-\d+@mailinator\.com\n", first.stdout)
    assert first.stdout != second.stdout


def test_an_empty_override_counts_as_no_override() -> None:
    result = run("bash", str(RECIPIENT_SCRIPT), recipient="")

    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith("@mailinator.com\n")


@pytest.mark.parametrize(
    "address",
    ["user@example.com", "First.Last+e2e@Sub.Example.co.uk", "a_b-c%d@mail-host.io"],
)
def test_an_override_is_used_exactly_as_given(address: str) -> None:
    result = run("bash", str(RECIPIENT_SCRIPT), recipient=address)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{address}\n"


@pytest.mark.parametrize(
    "address",
    [
        "not-an-email",
        "user@",
        "@example.com",
        "user@example",
        "user@@example.com",
        "user.@example.com",
        ".user@example.com",
        "us..er@example.com",
        "user@-example.com",
        "user@example..com",
        " user@example.com",
        "user@example.com ",
        "user name@example.com",
        "user@example.com\nother@example.com",
        "u'ser@example.com",
        'u"ser@example.com',
        "us\\er@example.com",
        f"{'a' * 65}@example.com",
        f"user@{'a' * 250}.com",
    ],
)
def test_a_malformed_override_is_rejected(address: str) -> None:
    result = run("bash", str(RECIPIENT_SCRIPT), recipient=address)

    assert result.returncode != 0
    assert result.stdout == ""
    assert RECIPIENT_OVERRIDE in result.stderr


def test_the_script_rejects_a_malformed_override_before_anything_else() -> None:
    # Checked even before the opt-in, so this needs no opt-in and cannot send anything.
    result = run("bash", str(E2E_SCRIPT), recipient="user@")

    assert result.returncode != 0
    assert RECIPIENT_OVERRIDE in result.stderr
    assert f"{OPT_IN}=1" not in result.stderr


@pytest.mark.parametrize("value", ["0", "-5", "abc", "1.5", "100000", " 60"])
def test_the_script_rejects_a_malformed_delivery_timeout_before_anything_else(
    value: str,
) -> None:
    result = run("bash", str(E2E_SCRIPT), extra={TIMEOUT_OVERRIDE: value})

    assert result.returncode != 0
    assert TIMEOUT_OVERRIDE in result.stderr
    assert f"{OPT_IN}=1" not in result.stderr
