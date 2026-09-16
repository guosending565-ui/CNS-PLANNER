"""Injectable exact-HTML PDF renderer."""

from __future__ import annotations

import os
from typing import Protocol


class PdfRendererUnavailable(RuntimeError):
    pass


class PdfRenderer(Protocol):
    def render(self, html_path, pdf_path) -> None: ...


class PlaywrightPdfRenderer:
    """Render the exact generated HTML to PDF through a Chromium browser.

    Browser selection:
    1. CNS_PDF_BROWSER_CHANNEL environment override
    2. Microsoft Edge on Windows
    3. Google Chrome
    4. Playwright bundled Chromium
    """

    def __init__(self, browser_channel=None):
        self.browser_channel = (
            browser_channel
            or os.environ.get("CNS_PDF_BROWSER_CHANNEL")
        )

    def render(self, html_path, pdf_path):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PdfRendererUnavailable(
                "pdf_renderer_unavailable：当前 CNS 服务使用的 Python "
                "环境未安装 Playwright。请在 QGIS Python 环境中安装 playwright。"
            ) from exc

        errors = []

        try:
            with sync_playwright() as runtime:
                browser = None

                candidates = []

                # Explicit user configuration has highest priority.
                if self.browser_channel:
                    candidates.append(self.browser_channel)

                # CNS-PLANNER currently targets Windows local deployment.
                if os.name == "nt":
                    candidates.extend(["msedge", "chrome"])
                else:
                    candidates.append("chrome")

                # Remove duplicates while preserving order.
                candidates = list(dict.fromkeys(candidates))

                # Try installed browsers first.
                for channel in candidates:
                    try:
                        browser = runtime.chromium.launch(
                            channel=channel,
                            headless=True,
                        )
                        break
                    except Exception as exc:
                        errors.append(f"{channel}: {exc}")

                # Finally try Playwright bundled Chromium if present.
                if browser is None:
                    try:
                        browser = runtime.chromium.launch(headless=True)
                    except Exception as exc:
                        errors.append(f"playwright-chromium: {exc}")

                if browser is None:
                    detail = " | ".join(errors)
                    raise PdfRendererUnavailable(
                        "pdf_renderer_unavailable：没有找到可用的 PDF 浏览器。"
                        "已尝试 Microsoft Edge、Google Chrome 和 "
                        f"Playwright Chromium。详细信息：{detail}"
                    )

                try:
                    page = browser.new_page()

                    page.goto(
                        html_path.resolve().as_uri(),
                        wait_until="load",
                    )

                    # Use print CSS from the same HTML source.
                    page.emulate_media(media="print")

                    page.pdf(
                        path=str(pdf_path),
                        print_background=True,
                        format="A4",
                        prefer_css_page_size=True,
                    )
                finally:
                    browser.close()

        except PdfRendererUnavailable:
            raise
        except Exception as exc:
            raise PdfRendererUnavailable(
                f"pdf_renderer_unavailable：PDF 渲染失败：{exc}"
            ) from exc