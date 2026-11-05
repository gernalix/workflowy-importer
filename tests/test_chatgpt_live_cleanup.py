import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from workflowy_importer.chatgpt_live import ChatGPTCloudCollector


class CollectorCleanupTests(unittest.TestCase):
    def driver(self, pw):
        return patch.dict(sys.modules, {
            "playwright.sync_api": SimpleNamespace(sync_playwright=lambda: SimpleNamespace(start=lambda: pw))})

    def test_connection_failure_stops_driver_without_closing_browser(self):
        pw = Mock()
        pw.chromium.connect_over_cdp.side_effect = RuntimeError("unavailable")
        collector = ChatGPTCloudCollector()
        with self.driver(pw), self.assertRaisesRegex(RuntimeError, "unavailable"):
            collector.__enter__()
        pw.stop.assert_called_once()
        self.assertIsNone(collector._pw)
        self.assertIsNone(collector._browser)

    def test_navigation_failure_closes_only_new_page_and_preserves_error(self):
        page = Mock()
        page.goto.side_effect = RuntimeError("navigation failed")
        personal = Mock(url="https://chatgpt.com/c/example")
        context = Mock(pages=[personal])
        context.new_page.return_value = page
        browser = Mock(contexts=[context])
        pw = Mock()
        pw.chromium.connect_over_cdp.return_value = browser
        pw.stop.side_effect = RuntimeError("cleanup failed")
        collector = ChatGPTCloudCollector()
        with self.driver(pw), self.assertRaisesRegex(RuntimeError, "navigation failed"):
            collector.__enter__()
        page.close.assert_called_once()
        personal.close.assert_not_called()
        browser.close.assert_not_called()
        self.assertIsNone(collector._pw)

    def test_success_keeps_existing_page_and_disconnects_driver(self):
        page = Mock(url="https://chatgpt.com/")
        context = Mock(pages=[page])
        pw = Mock()
        pw.chromium.connect_over_cdp.return_value = Mock(contexts=[context])
        with self.driver(pw), ChatGPTCloudCollector() as collector:
            self.assertIs(collector._page, page)
        page.close.assert_not_called()
        context.new_page.assert_not_called()
        pw.stop.assert_called_once()
