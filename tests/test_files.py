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


def test_everything_else_becomes_an_attachment(tmp_path):
    folder = copy_observation(tmp_path / "obs")
    (folder / "_notes.txt").write_text("for the reviewer\n")
    (folder / "finder_chart.png").write_bytes(b"\x89PNG")
    (folder / "X_Tangra.lc").write_text("lc\n")
    (folder / ".DS_Store").write_bytes(b"junk")
    (folder / "raw").mkdir()
    (folder / "raw" / "frame.fits").write_bytes(b"x")

    mapping = map_folder(folder)

    assert set(mapping.slots) == {"report", "lightcurve", "log"}
    # Hidden files and subdirectories are never attached.
    assert [path.name for path in mapping.attachments] == [
        "X_Tangra.lc",
        "_notes.txt",
        "finder_chart.png",
    ]
    assert mapping.ignored == []


def test_no_attachments_leaves_them_out_and_reports_them(tmp_path):
    folder = copy_observation(tmp_path / "obs")
    (folder / "_notes.txt").write_text("for the reviewer\n")
    (folder / "finder_chart.png").write_bytes(b"\x89PNG")

    mapping = map_folder(folder, attachments=False)

    assert mapping.attachments == []
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
    assert [path.name for path in mapping.attachments] == [
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


# -- attachments ------------------------------------------------------------


def test_attach_adds_a_file_from_elsewhere_once(tmp_path):
    folder = copy_observation(tmp_path / "obs")
    (folder / "plot.png").write_bytes(b"png")
    outside = tmp_path / "extra.pdf"
    outside.write_bytes(b"%PDF")

    mapping = map_folder(
        folder,
        attach=[outside, folder / "plot.png", folder / "../obs/plot.png"],
    )

    assert [path.name for path in mapping.attachments] == ["plot.png", "extra.pdf"]


def test_attach_is_honoured_with_no_attachments(tmp_path):
    folder = copy_observation(tmp_path / "obs")
    (folder / "plot.png").write_bytes(b"png")
    outside = tmp_path / "extra.pdf"
    outside.write_bytes(b"%PDF")

    mapping = map_folder(folder, attachments=False, attach=[outside])

    assert [path.name for path in mapping.attachments] == ["extra.pdf"]
    assert [path.name for path in mapping.ignored] == ["plot.png"]


def test_attach_of_a_missing_file_is_missing_files(tmp_path):
    folder = copy_observation(tmp_path / "obs")

    with pytest.raises(MappingError) as excinfo:
        map_folder(folder, attach=[tmp_path / "nope.png"])

    assert excinfo.value.code == "missing_files"


@pytest.mark.parametrize("name", ["reduce.py", "setup.EXE", "lib.so"])
def test_a_blocked_type_fails_the_whole_folder_and_names_it(tmp_path, name):
    folder = copy_observation(tmp_path / "obs")
    (folder / "plot.png").write_bytes(b"png")
    (folder / name).write_bytes(b"x")

    with pytest.raises(MappingError) as excinfo:
        map_folder(folder)

    error = excinfo.value
    assert error.code == "blocked_attachment"
    assert error.details["files"] == [name]
    assert name in error.message
    assert "--no-attachments" in error.hint
    # The same folder maps once the attachments are left out.
    assert map_folder(folder, attachments=False).ignored


def test_trailing_dots_and_spaces_do_not_hide_a_blocked_type():
    """The server strips them (Windows does on save); so does the check.

    Checked on a bare path: Windows would strip the name on disk already.
    """
    from pathlib import Path

    from iota_hub.files import check_attachments

    with pytest.raises(MappingError) as excinfo:
        check_attachments([Path("run.bat .")])

    assert excinfo.value.code == "blocked_attachment"


def test_an_oversized_attachment_names_the_file_and_the_limit(tmp_path, monkeypatch):
    from iota_hub import files

    monkeypatch.setattr(files, "ATTACHMENT_MAX_BYTES", 10)
    folder = copy_observation(tmp_path / "obs")
    (folder / "small.png").write_bytes(b"x" * 10)
    (folder / "big.lc").write_bytes(b"x" * 11)

    with pytest.raises(MappingError) as excinfo:
        map_folder(folder)

    error = excinfo.value
    assert error.code == "attachment_too_large"
    assert error.details == {"files": ["big.lc"], "max_size_bytes": 10}
    assert "big.lc" in error.message


def test_too_many_attachments_names_them_and_the_limit(tmp_path):
    from iota_hub.files import MAX_ATTACHMENTS

    folder = copy_observation(tmp_path / "obs")
    for index in range(MAX_ATTACHMENTS + 1):
        (folder / f"plot_{index:02d}.png").write_bytes(b"png")

    with pytest.raises(MappingError) as excinfo:
        map_folder(folder)

    error = excinfo.value
    assert error.code == "too_many_attachments"
    assert error.details["max_allowed"] == MAX_ATTACHMENTS
    assert error.details["count"] == MAX_ATTACHMENTS + 1
    assert len(error.details["files"]) == MAX_ATTACHMENTS + 1


def test_exactly_the_maximum_is_accepted(tmp_path):
    from iota_hub.files import MAX_ATTACHMENTS

    folder = copy_observation(tmp_path / "obs")
    for index in range(MAX_ATTACHMENTS):
        (folder / f"plot_{index:02d}.png").write_bytes(b"png")

    assert len(map_folder(folder).attachments) == MAX_ATTACHMENTS
