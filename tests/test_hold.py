"""The docked page's /hold image: keeps the page loading until the dock releases it."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from intelligent_iterm.web import EventHub, render_page  # noqa: E402


class HoldTest(unittest.TestCase):
    def test_only_docked_pages_get_the_hold_image(self):
        docked = render_page(None, {"t": ["tok"], "dock": ["1"], "tab": ["T 1"]}).decode()
        self.assertIn('src="/hold?t=tok&amp;tab=T+1"', docked)
        self.assertNotIn("/hold", render_page(None, {"t": ["tok"]}).decode())

    def test_hold_waits_for_release_and_is_immediate_otherwise(self):
        async def run():
            hub = EventHub()
            await asyncio.wait_for(hub.wait_hold("A", 5), 0.5)  # nothing armed
            hub.hold("A")
            waiter = asyncio.ensure_future(hub.wait_hold("A", 5))
            await asyncio.sleep(0.05)
            self.assertFalse(waiter.done())
            hub.release("A")
            await asyncio.wait_for(waiter, 0.5)
            hub.hold("B")
            await hub.wait_hold("B", 0.05)  # times out and disarms
            await asyncio.wait_for(hub.wait_hold("B", 5), 0.5)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
