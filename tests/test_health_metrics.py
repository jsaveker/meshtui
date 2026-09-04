"""Last-heard health bands, SNR trend, and rolling one-hour airtime."""

from meshtui.model import Node
from meshtui.state import Stats
from meshtui.widgets.nodes import (NodeTable, compact_node_name, fmt_age,
                                   snr_spark, stable_table_text)

failures = []


def check(name, got, want):
    print(f"  {'ok  ' if got == want else 'FAIL'} {name}")
    if got != want:
        failures.append(name)


check("fresh node is green", str(fmt_age(30).style), "green")
check("aging node is amber", str(fmt_age(1200).style), "yellow")
check("old node is ghosted", str(fmt_age(7200).style), "grey54")
spark = snr_spark([-18, -8, 2, 8])
check("SNR sparkline keeps one glyph per sample", len(str(spark).strip()), 4)

# A terminal may render emoji at a different width than Rich expects. Keep
# arbitrary Unicode in full-detail views, but make the compact row cell-stable
# so Hop/Bat/Pkt/Age cannot drift sideways.
now = 10000.0
radio = Node(num=1, node_id="!00000001", short_name="K5DG",
             long_name="K5DG-1⚡", hops=4, packets=2,
             last_heard=now - 3600)
compact = compact_node_name(radio)
check("compact names replace variable-width glyphs", compact.plain,
      "K5DG  K5DG-1*   ")
check("compact names preserve international letters",
      stable_table_text("Rádio 東京 Радио ⚡"), "Rádio 東京 Радио *")
check("compact names preserve combining scripts",
      stable_table_text("हिन्दी"), "हिन्दी")
check("compact name occupies exactly sixteen stable cells",
      compact.cell_len == len(compact.plain) == 16, True)
cells = NodeTable()._cells(radio, now)
check("unicode names cannot shift later node columns",
      tuple(cell.plain for cell in cells[4:]), ("4", "-", "2", "1h"))

stats = Stats()
stats.record_radio_airtime({"tx_air_secs": 0, "rx_air_secs": 0}, ts=0)
stats.record_radio_airtime({"tx_air_secs": 90, "rx_air_secs": 90}, ts=1800)
stats.record_radio_airtime({"tx_air_secs": 180, "rx_air_secs": 180}, ts=3600)
check("one-hour cumulative counters become utilization",
      round(stats.airtime_last_hour(), 1), 10.0)
stats.record_radio_airtime({"tx_air_secs": 360, "rx_air_secs": 360}, ts=5400)
check("rolling window discards the first half-hour",
      round(stats.airtime_last_hour(), 1), 15.0)
stats.record_radio_airtime({"tx_air_secs": 1, "rx_air_secs": 1}, ts=5500)
check("radio counter reset starts a new window", stats.airtime_last_hour(), None)

if failures:
    print("\nFAIL:", failures)
    raise SystemExit(1)
print("\nPASS")
