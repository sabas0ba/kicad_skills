import struct

import numpy as np
import pytest

from eda_toolkit.spice import rawfile

ASCII_RAW = """Title: * test deck
Date: Sat Jan 1 00:00:00 2024
Plotname: DC transfer characteristic
Flags: real
No. Variables: 2
No. Points: 3
Variables:
\t0\tv-sweep\tvoltage
\t1\tv(out)\tvoltage
Values:
0\t0.0
\t0.5
1\t1.0
\t1.5
2\t2.0
\t2.5
"""


def binary_raw(plotname="Transient Analysis", flags="real", points=4):
    header = (
        f"Title: * binary deck\nDate: today\nPlotname: {plotname}\nFlags: {flags}\n"
        f"No. Variables: 2\nNo. Points: {points}\n"
        "Variables:\n\t0\ttime\ttime\n\t1\tv(out)\tvoltage\nBinary:\n"
    ).encode()
    payload = b""
    for i in range(points):
        if flags == "complex":
            payload += struct.pack("<dddd", float(i), 0.0, float(i) * 2, float(i))
        else:
            payload += struct.pack("<dd", float(i), float(i) * 2)
    return header + payload


def test_parse_ascii_real():
    plots = rawfile.parse_bytes(ASCII_RAW.encode())
    assert len(plots) == 1
    plot = plots[0]
    assert plot.analysis == "dc"
    assert plot.sweep == "v-sweep"
    assert plot.signals() == ["v(out)"]
    assert np.allclose(plot.data["v-sweep"], [0.0, 1.0, 2.0])
    assert np.allclose(plot.data["v(out)"], [0.5, 1.5, 2.5])


def test_parse_binary_real():
    plot = rawfile.parse_bytes(binary_raw())[0]
    assert plot.analysis == "tran"
    assert plot.points == 4
    assert np.allclose(plot.data["v(out)"], [0.0, 2.0, 4.0, 6.0])
    assert not plot.complex


def test_parse_binary_complex():
    plot = rawfile.parse_bytes(binary_raw("AC Analysis", "complex", 3))[0]
    assert plot.analysis == "ac"
    assert plot.complex
    assert plot.data["v(out)"][1] == complex(2.0, 1.0)


def test_multiple_plots_in_one_file():
    blob = binary_raw("AC Analysis", "complex", 2) + b"\n" + binary_raw()
    plots = rawfile.parse_bytes(blob)
    assert [p.analysis for p in plots] == ["ac", "tran"]


def test_truncated_binary_raises():
    blob = binary_raw()[:-8]
    with pytest.raises(rawfile.RawFileError):
        rawfile.parse_bytes(blob)


def test_missing_data_section_raises():
    with pytest.raises(rawfile.RawFileError):
        rawfile.parse_bytes(b"Title: x\nPlotname: y\nNo. Variables: 1\nNo. Points: 1\n")


def test_to_csv_expands_complex_columns(tmp_path):
    plot = rawfile.parse_bytes(binary_raw("AC Analysis", "complex", 3))[0]
    dest = rawfile.to_csv(plot, tmp_path / "ac.csv")
    header = dest.read_text().splitlines()[0].split(",")
    assert "v(out)_mag" in header and "v(out)_deg" in header
    assert len(dest.read_text().splitlines()) == 4


def test_to_csv_real(tmp_path):
    plot = rawfile.parse_bytes(ASCII_RAW.encode())[0]
    dest = rawfile.to_csv(plot, tmp_path / "dc.csv")
    assert dest.read_text().splitlines()[0] == "v-sweep,v(out)"
