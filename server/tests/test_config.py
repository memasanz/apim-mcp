import json
from pathlib import Path

import pytest

from app.config import AppConfig, ConfigError, ConfigStore, load_config, write_config

SAMPLE = Path(__file__).resolve().parents[2] / "config" / "config.sample.json"


def test_sample_config_loads() -> None:
    cfg = load_config(SAMPLE)
    assert "apim" in cfg.services
    assert cfg.defaults.timeoutSeconds == 60


def test_iter_enabled_skips_disabled(tmp_path: Path) -> None:
    cfg = load_config(SAMPLE)
    enabled = cfg.iter_enabled_commands()
    # All sample commands are read-only and enabled, except create/update/delete on api.
    names = {(s, r, c) for s, r, c, _ in enabled}
    assert ("apim", "api", "list") in names
    assert ("apim", "api", "create") not in names


def test_invalid_az_command_rejected(tmp_path: Path) -> None:
    bad = {
        "version": 1,
        "services": {
            "x": {
                "resources": {
                    "y": {
                        "commands": {
                            "z": {"enabled": True, "azCommand": ["foo; rm -rf /"]}
                        }
                    }
                }
            }
        },
    }
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ConfigError):
        load_config(p)


def test_write_then_reload_roundtrip(tmp_path: Path) -> None:
    src = json.loads(SAMPLE.read_text())
    p = tmp_path / "c.json"
    p.write_text(json.dumps(src))
    cfg = load_config(p)
    cfg.services["apim"].resources["api"].commands["create"].enabled = True
    write_config(p, cfg)
    reloaded = load_config(p)
    assert reloaded.services["apim"].resources["api"].commands["create"].enabled


@pytest.mark.asyncio
async def test_store_subscribe_on_replace(tmp_path: Path) -> None:
    src = SAMPLE.read_text()
    p = tmp_path / "c.json"
    p.write_text(src)
    store = ConfigStore(p)
    received: list[AppConfig] = []

    async def listener(cfg: AppConfig) -> None:
        received.append(cfg)

    store.subscribe(listener)
    new = store.config.model_copy(deep=True)
    new.defaults.timeoutSeconds = 30
    await store.replace(new)
    assert received and received[0].defaults.timeoutSeconds == 30


def test_governance_fields_roundtrip(tmp_path: Path) -> None:
    bad_pii = {
        "version": 1,
        "services": {
            "x": {
                "resources": {
                    "y": {
                        "commands": {
                            "z": {
                                "enabled": True,
                                "azCommand": ["account", "show"],
                                "piiRisk": "extreme",
                            }
                        }
                    }
                }
            }
        },
    }
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad_pii))
    with pytest.raises(ConfigError):
        load_config(p)

    good = json.loads(json.dumps(bad_pii))
    good["services"]["x"]["resources"]["y"]["commands"]["z"].update(
        {
            "piiRisk": "high",
            "rbacRoles": ["Reader", "Log Analytics Reader"],
            "piiNotes": "May return request bodies if verbose logging is on.",
        }
    )
    q = tmp_path / "good.json"
    q.write_text(json.dumps(good))
    cfg = load_config(q)
    cmd = cfg.services["x"].resources["y"].commands["z"]
    assert cmd.piiRisk == "high"
    assert cmd.rbacRoles == ["Reader", "Log Analytics Reader"]
    write_config(q, cfg)
    again = load_config(q)
    assert again.services["x"].resources["y"].commands["z"].piiRisk == "high"
