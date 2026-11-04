"""Tests for the unified 5-organ NeckLink Fruit-Fly Brain Brand Suite (`necklink.suite`)."""
import pytest

import necklink
from necklink import cli


def _require_suite_organs() -> None:
    for mod in ("necksense", "neckcompass", "necksight", "neckcord"):
        pytest.importorskip(mod)


def test_brand_suite_catalog_and_cli_base(capsys):
    """Catalog and `nltp products` work both standalone and with `[suite]` installed."""
    assert len(necklink.NECKLINK_SUITE_CATALOG) == 5
    status = necklink.check_suite_installed()
    assert status.get("pynltp") != "not-installed"

    rc = cli.main(["products"])
    assert rc == 0
    out = capsys.readouterr().out
    for label in ("NLTP", "NeckSense", "NeckCompass", "NeckSight", "NeckCord"):
        assert label in out


def test_brand_suite_lazy_organ_imports():
    """Verify `from necklink import nltp, sense, compass, sight, cord` resolves all 5 organs."""
    _require_suite_organs()
    from necklink import compass, cord, nltp, sense, sight

    assert hasattr(nltp, "NLTPClient")
    assert hasattr(sense, "KenyonHasher")
    assert hasattr(compass, "RingCompass")
    assert hasattr(sight, "NeckSightPipeline")
    assert hasattr(cord, "ShuntingThrusterCord")


def test_brand_suite_all_organs_installed():
    _require_suite_organs()
    status = necklink.check_suite_installed()
    for pkg in ("pynltp", "necksense", "neckcompass", "necksight", "neckcord"):
        assert status.get(pkg) != "not-installed"


def test_closed_loop_5_organ_autonomous_reflex_demo():
    _require_suite_organs()
    res = necklink.run_autonomous_reflex_demo(loss=0.15, seed=42)
    assert res["compass_spike_error_deg"] < 2.0
    assert res["sense_background_novelty"] < 0.15
    assert res["sense_plume_anomaly_novelty"] > 0.80
    assert res["sight_glare_50x_false_alarm"] is False
    assert res["sight_looming_triggered"] is True
    assert res["cord_fouled_thruster_cmd_N"] < 0.5
    assert res["nltp_reflex_received"] is True
    assert res["nltp_4b_command_received"] is True
    assert res["nltp_file_sha256_verified"] is True
