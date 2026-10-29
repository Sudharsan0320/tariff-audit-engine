import pytest

from src.parse_rider import (
    parse_rider_components,
    parse_special_rider,
    parse_table_rider,
    parse_uniform_rider,
)


def test_uniform_rider_normalizes_cents_for_all_listed_schedules():
    text = (
        "The charge for service under Rate Schedules 100, 102, 110, 120, "
        "130, 131, 132, 134, 150, 151, 154, SMH, and SSL shall be increased "
        "by 2.0968 cents per kilowatt-hour."
    )

    components = parse_uniform_rider("A", [text])

    assert len(components) == 13
    assert {row[1] for row in components} == {0.020968}
    assert {row[2] for row in components} == {"usd_per_kwh"}


def test_table_rider_reads_energy_and_demand_cells_and_ignores_footnotes():
    class Page:
        def extract_tables(self):
            rows = [
                ["Schedule", "Energy", "Demand"],
                ["100", "0.4320¢/kWh", ""],
                ["132", "0.3950¢/kWh", ""],
                ["130", "", "$1.340/kW 2"],
            ]
            rows.extend(
                [schedule, "0.4320¢/kWh", ""]
                for schedule in (
                    "102",
                    "110",
                    "120",
                    "131",
                    "134",
                    "150",
                    "151",
                    "154",
                    "SMH",
                    "SSL",
                )
            )
            return [rows]

    rows = parse_table_rider("GEN-CM", [(104, Page(), "")])

    assert len(rows) == 13
    assert [(row[2], row[3]) for row in rows[:3]] == [
        ("usd_per_kwh", "100"),
        ("usd_per_kwh", "132"),
        ("usd_per_kw", "130"),
    ]
    assert [row[1] for row in rows[:3]] == pytest.approx(
        [0.00432, 0.00395, 1.34]
    )


def test_special_credit_riders_store_negative_rates():
    rider_k = (
        "RIDER K. This rider applies to electric services billed under "
        "Schedules 100, 130, and 131. III. MONTHLY CREDIT multiplied by: "
        "0.7¢ Credit per kWh"
    )
    rider_l = (
        "RIDER L. This rider applies to electric services billed under "
        "Schedules 100 and 130. III. MONTHLY CREDIT multiplied by: "
        "0.22¢ Credit per kWh"
    )

    k_rows = parse_special_rider("K", [rider_k])
    l_rows = parse_special_rider("L", [rider_l])

    assert [(row[3]) for row in k_rows] == ["100", "130", "131"]
    assert [row[1] for row in k_rows] == pytest.approx(
        [-0.007, -0.007, -0.007]
    )
    assert [row[3] for row in l_rows] == ["100", "130"]
    assert [row[1] for row in l_rows] == pytest.approx([-0.0022, -0.0022])


def test_special_rec_rider_reads_monthly_rate_and_companion_schedules():
    text = (
        "who also purchases Electricity Supply Service in accordance with "
        "either Schedule 100, "
        "110, 120, 130, 131, 132, or 134. The Monthly Rate for RECs (MRR) "
        "equals 0.595 cents per kWh."
    )

    rows = parse_special_rider("G-CM", [text])

    assert len(rows) == 7
    assert [row[1] for row in rows] == pytest.approx([0.00595] * 7)
    assert {row[3] for row in rows} == {
        "100",
        "110",
        "120",
        "130",
        "131",
        "132",
        "134",
    }


def test_rider_parser_dispatches_by_rider_type():
    text = (
        "Rate Schedules 100, 102, 110, 120, 130, 131, 132, 134, 150, 151, "
        "154, SMH, and SSL shall be increased by 0.000 cents per kilowatt-hour."
    )
    rows = parse_rider_components("PIPP-CM", [(109, None, text)])

    assert len(rows) == 13
    assert {row[1] for row in rows} == {0.0}
