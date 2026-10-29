"""Screenshot the dashboard tabs and the HTML report for the README, into docs/img/.

    uv run streamlit run app/dashboard.py --server.port 8711 --server.headless true
    uv run flowlens report --repo streamlit/streamlit
    python3 -m http.server 8710 --directory site
    uv run --with playwright python scripts/screenshots.py

``scripts/screenshots.py insights`` takes only the report's charts that docs/INSIGHTS.md
shows, one card each, and needs only the report server.

Drives the installed Google Chrome (Playwright's ``channel="chrome"``), so there's no
browser to download. The dashboard opens a tab from its URL (``?tab=DORA``).
"""

import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

DASHBOARD = "http://localhost:8711"
REPORT = "http://localhost:8710"
OUT = Path(__file__).parent.parent / "docs" / "img"
TABS = {"Flow": "flow", "Forecast": "forecast", "DORA": "dora", "PR flow": "pr-flow"}
SIZE = {"width": 1440, "height": 1500}
# Report chart → image for docs/INSIGHTS.md. At 900px the report lays charts one per row.
INSIGHT_CHARTS = {
    "dora-lead_time": "insight-lead-time",
    "dora-recovery": "insight-recovery",
    "pr-flow-pr_flow": "insight-pr-flow",
    "forecast-calibration": "insight-calibration",
}


def settle(page: Page, plots: int) -> None:
    """Wait for the charts to draw and Streamlit to stop running."""
    page.wait_for_function(
        f"document.querySelectorAll('.js-plotly-plot .main-svg').length >= {plots}",
        timeout=60_000,
    )
    page.wait_for_function(
        "!document.querySelector('[data-testid=stStatusWidget]')", timeout=60_000
    )
    page.wait_for_timeout(1_500)  # plotly's resize and the fade-in


def main(dashboard: str = DASHBOARD, report: str = REPORT) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome")
        page = browser.new_page(viewport=SIZE, device_scale_factor=2, color_scheme="light")
        for tab, name in TABS.items():
            page.goto(f"{dashboard}/?tab={tab.replace(' ', '+')}")
            settle(page, 1)
            page.screenshot(path=OUT / f"dashboard-{name}.png")
            print(f"Wrote {OUT / f'dashboard-{name}.png'}")
        page.set_viewport_size({"width": 1280, "height": 1700})
        page.goto(report)
        settle(page, 4)
        page.screenshot(path=OUT / "report.png")
        print(f"Wrote {OUT / 'report.png'}")
        mobile = browser.new_page(
            viewport={"width": 390, "height": 1100}, device_scale_factor=2, is_mobile=True
        )
        mobile.goto(report)
        settle(mobile, 2)
        mobile.screenshot(path=OUT / "report-mobile.png")
        print(f"Wrote {OUT / 'report-mobile.png'}")
        browser.close()


def insights(report: str = REPORT) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome")
        page = browser.new_page(
            viewport={"width": 900, "height": 1200}, device_scale_factor=2, color_scheme="light"
        )
        page.goto(report)
        settle(page, len(INSIGHT_CHARTS))
        for plot, name in INSIGHT_CHARTS.items():
            card = page.locator(f".card:has(#plot-{plot})")
            card.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            card.screenshot(path=OUT / f"{name}.png")
            print(f"Wrote {OUT / f'{name}.png'}")
        browser.close()


if __name__ == "__main__":
    if sys.argv[1:2] == ["insights"]:
        insights(*sys.argv[2:])
    else:
        main(*sys.argv[1:])
