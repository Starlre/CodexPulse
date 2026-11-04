"""Tk glyph-bound regressions; run on a desktop with Tk available."""
import itertools
import tkinter as tk
import unittest
from types import SimpleNamespace
from metrics import Turn
from pulse import Pulse, ZoomCanvas, KEY


class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()
        cls.app = Pulse.__new__(Pulse)
        app = cls.app
        app.root = cls.root
        app.canvas = ZoomCanvas(cls.root, highlightthickness=0)
        app.photo = tk.PhotoImage(master=cls.root, width=1, height=1)
        app.pet_frame = lambda: app.photo
        app.auto = tk.BooleanVar(master=cls.root, value=True)
        app.zoom_percent = tk.IntVar(master=cls.root, value=100)
        app.status_note = ''
        app.selected = ''

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def boxes(self):
        c = self.app.canvas
        return [(c.itemcget(item, 'text'), c.bbox(item))
                for item in c.find_all() if c.type(item) == 'text']

    def assert_layout(self):
        app = self.app
        boxes = self.boxes()
        width, height = app.dimensions()
        for text, box in boxes:
            self.assertTrue(0 <= box[0] < box[2] <= width and
                            0 <= box[1] < box[3] <= height, (text, box, width, height))
        for (left, a), (right, b) in itertools.combinations(boxes, 2):
            overlap = min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])
            self.assertFalse(overlap, (left, a, right, b))

    def test_all_zooms_dpi_states_and_long_values(self):
        app = self.app
        cases = [None,
                 Turn('normal', 1, status='complete', duration=50.3, ttft=3.3, output_tokens=2486,
                      input_tokens=10000,cached_input_tokens=8000),
                 Turn('long', 1, status='complete', duration=123456.7, ttft=12345.6, output_tokens=123456789012),
                 Turn('running', 1, duration=88.8, output_tokens=999999999),
                 Turn('interrupted', 1, status='interrupted', duration=80.7)]
        for collapsed, percent, scaling, turn in itertools.product(
                (False, True), (75, 100, 125, 150, 175, 200), (1.0, 96/72, 2.0, 2.5), cases):
            with self.subTest(compact=collapsed, zoom=percent, dpi=scaling, case=turn.id if turn else 'empty'):
                self.root.tk.call('tk', 'scaling', scaling)
                app.collapsed = collapsed
                app.zoom = app.canvas.zoom = percent / 100
                app.zoom_percent.set(percent)
                app.sessions = [SimpleNamespace(id='01a1a65e-test', turns=[turn], summary=lambda: dict(
                    completed=999999, avg_ttft=12345.6, avg_duration=123456.7, avg_rate=123456.7))] if turn else []
                app.draw()
                self.assert_layout()

    def test_font_bounds_do_not_change_with_system_scale(self):
        app = self.app
        app.collapsed = False
        app.zoom = app.canvas.zoom = 1.5
        app.zoom_percent.set(150)
        app.sessions = []
        baseline = None
        for scaling in (1.0, 96/72, 2.0, 2.5):
            self.root.tk.call('tk', 'scaling', scaling)
            app.draw()
            current = self.boxes()
            if baseline is None:
                baseline = current
            self.assertEqual(baseline, current)


if __name__ == '__main__':
    unittest.main()
