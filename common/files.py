from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable

from common.models import AccountConfig


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", delete=False, encoding="utf-8", dir=path.parent) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    temp_path.replace(path)


def write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_accounts(accounts_dir: Path) -> list[AccountConfig]:
    accounts: list[AccountConfig] = []
    for path in sorted(accounts_dir.glob("acc_*.json")):
        accounts.append(AccountConfig.model_validate_json(path.read_text(encoding="utf-8-sig")))
    return accounts


def save_account(accounts_dir: Path, account: AccountConfig) -> Path:
    path = accounts_dir / f"{account.id}.json"
    write_json(path, account.model_dump(mode="json"))
    return path


def save_accounts_bulk(accounts_dir: Path, accounts: list[AccountConfig]) -> list[Path]:
    accounts_dir.mkdir(parents=True, exist_ok=True)
    existing = {path.stem: path for path in accounts_dir.glob("acc_*.json")}
    saved_paths: list[Path] = []
    active_ids: set[str] = set()
    for account in accounts:
        active_ids.add(account.id)
        saved_paths.append(save_account(accounts_dir, account))
    for account_id, path in existing.items():
        if account_id not in active_ids:
            path.unlink(missing_ok=True)
    return saved_paths


def load_accounts_payload(accounts_dir: Path) -> list[dict[str, Any]]:
    return [account.model_dump(mode="json") for account in load_accounts(accounts_dir)]


def update_env_value(env_path: Path, key: str, value: str) -> None:
    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    updated = False
    output: list[str] = []
    for line in existing_lines:
        if line.startswith(f"{key}="):
            output.append(f"{key}={value}")
            updated = True
        else:
            output.append(line)
    if not updated:
        output.append(f"{key}={value}")
    atomic_write_text(env_path, "\n".join(output).strip() + "\n")


def ensure_directories(paths: Iterable[Path]) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
