"""Screenshot the dashboard tabs and the HTML report for the README, into docs/img/.

    uv run streamlit run app/dashboard.py --server.port 8711 --server.headless true
    uv run flowlens report --repo streamlit/streamlit
    python3 -m http.server 8710 --directory site
    uv run --with playwright python scripts/screenshots.py

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


if __name__ == "__main__":
    main(*sys.argv[1:])
