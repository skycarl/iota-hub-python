"""Folder → slot mapping: the rules, and every way they refuse to guess."""

from __future__ import annotations

import pytest
from conftest import FIXTURES

from iota_hub import MappingError, map_folder

OBSERVATION = FIXTURES / "observation"


def copy_observation(destination):
    """The shared fixture folder, copied somewhere a test can add files."""
    destination.mkdir(parents=True, exist_ok=True)
    for source in sorted(OBSERVATION.iterdir()):
        (destination / source.name).write_bytes(source.read_bytes())
    return destination


# -- the happy path ---------------------------------------------------------


def test_maps_the_shared_fixture_folder():
    mapping = map_folder(OBSERVATION)

    assert set(mapping.slots) == {"report", "lightcurve", "log"}
    assert mapping.slots["report"].suffix == ".xlsx"
    assert mapping.slots["lightcurve"].suffix == ".csv"
    assert mapping.slots["log"].name.endswith("_pyote_log.txt")
    assert mapping.ignored == []
    assert mapping.missing_required() == []


def test_vizier_is_optional_and_mapped_when_present(tmp_path):
    folder = copy_observation(tmp_path / "obs")
    (folder / "20180305_9721.dat").write_text("vizier\n")

    mapping = map_folder(folder)

    assert mapping.slots["vizier"].name == "20180305_9721.dat"
    assert mapping.ignored == []


def test_everything_else_is_ignored_not_uploaded(tmp_path):
    folder = copy_observation(tmp_path / "obs")
    (folder / "_notes.txt").write_text("for the reviewer\n")
    (folder / "finder_chart.png").write_bytes(b"\x89PNG")
    (folder / ".DS_Store").write_bytes(b"junk")
    (folder / "raw").mkdir()

    mapping = map_folder(folder)

    assert set(mapping.slots) == {"report", "lightcurve", "log"}
    assert sorted(path.name for path in mapping.ignored) == [
        "_notes.txt",
        "finder_chart.png",
    ]


def test_extensions_are_case_insensitive(tmp_path):
    folder = tmp_path / "obs"
    folder.mkdir()
    (folder / "report.XLSX").write_bytes(b"x")
    (folder / "curve.CSV").write_text("t,v\n")
    (folder / "PYOTE_LOG.TXT").write_text("log\n")

    mapping = map_folder(folder)

    assert mapping.slots["report"].name == "report.XLSX"
    assert mapping.slots["lightcurve"].name == "curve.CSV"
    assert mapping.slots["log"].name == "PYOTE_LOG.TXT"


# -- refusing to guess ------------------------------------------------------


def test_two_light_curves_are_ambiguous_and_name_both(tmp_path):
    folder = copy_observation(tmp_path / "obs")
    (folder / "second_run.csv").write_text("t,v\n")

    with pytest.raises(MappingError) as excinfo:
        map_folder(folder)

    error = excinfo.value
    assert error.code == "ambiguous_files"
    assert error.details["slot"] == "lightcurve"
    assert sorted(error.details["candidates"]) == [
        "20180305_9721_Doty_Observer_POS.csv",
        "second_run.csv",
    ]
    assert "--lightcurve" in error.hint


def test_two_reports_point_at_the_multi_station_limitation(tmp_path):
    folder = copy_observation(tmp_path / "obs")
    (folder / "20180305_9721_Doty_Observer_POS-2.xlsx").write_bytes(b"x")

    with pytest.raises(MappingError) as excinfo:
        map_folder(folder)

    error = excinfo.value
    assert error.code == "ambiguous_files"
    assert error.details["slot"] == "report"
    assert "--report" in error.hint
    assert "not supported yet" in error.hint


def test_a_missing_log_is_missing_files(tmp_path):
    folder = copy_observation(tmp_path / "obs")
    next(folder.glob("*_log.txt")).unlink()

    with pytest.raises(MappingError) as excinfo:
        map_folder(folder)

    error = excinfo.value
    assert error.code == "missing_files"
    assert error.details["missing"] == ["log"]
    assert "--log PATH" in error.hint


def test_a_txt_without_log_in_its_name_is_ignored_not_mapped(tmp_path):
    folder = copy_observation(tmp_path / "obs")
    next(folder.glob("*_log.txt")).rename(folder / "session_notes.txt")

    with pytest.raises(MappingError) as excinfo:
        map_folder(folder)

    assert excinfo.value.code == "missing_files"
    assert map_folder(folder, log=folder / "session_notes.txt").ignored == []


def test_a_directory_that_is_not_one(tmp_path):
    with pytest.raises(MappingError) as excinfo:
        map_folder(tmp_path / "nowhere")

    assert excinfo.value.code == "not_a_directory"


# -- explicit overrides -----------------------------------------------------


def test_an_explicit_path_wins_over_the_rule(tmp_path):
    folder = copy_observation(tmp_path / "obs")
    chosen = folder / "second_run.csv"
    chosen.write_text("t,v\n")

    mapping = map_folder(folder, lightcurve=chosen)

    assert mapping.slots["lightcurve"] == chosen
    assert [path.name for path in mapping.ignored] == [
        "20180305_9721_Doty_Observer_POS.csv"
    ]


def test_an_explicit_path_may_come_from_outside_the_folder(tmp_path):
    folder = copy_observation(tmp_path / "obs")
    outside = tmp_path / "elsewhere.csv"
    outside.write_text("t,v\n")

    mapping = map_folder(folder, lightcurve=outside)

    assert mapping.slots["lightcurve"] == outside


def test_an_explicit_path_is_checked_for_its_extension(tmp_path):
    folder = copy_observation(tmp_path / "obs")

    with pytest.raises(MappingError) as excinfo:
        map_folder(folder, lightcurve=folder / "20180305_9721_Doty_Observer_POS.xlsx")

    error = excinfo.value
    assert error.code == "invalid_extension"
    assert error.details["allowed_extensions"] == [".csv"]


def test_an_explicit_path_that_is_not_there(tmp_path):
    folder = copy_observation(tmp_path / "obs")

    with pytest.raises(MappingError) as excinfo:
        map_folder(folder, log=folder / "absent_log.txt")

    assert excinfo.value.code == "missing_files"
