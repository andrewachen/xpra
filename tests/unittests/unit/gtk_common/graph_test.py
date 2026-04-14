#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.
# ABOUTME: Tests for dual Y axis, step-function rendering, and event markers
# ABOUTME: in the Cairo graph widget used by session info.

import unittest
import struct
import cairo

from xpra.gtk.graph import make_graph_imagesurface


def _pixel_at(surface: cairo.ImageSurface, x: int, y: int) -> tuple[int, int, int]:
    """Read the RGB value of a pixel from a Cairo RGB24 surface."""
    data = surface.get_data()
    stride = surface.get_stride()
    # RGB24 format: 4 bytes per pixel (B, G, R, padding) in little-endian
    offset = y * stride + x * 4
    b, g, r, _ = struct.unpack_from("BBBB", data, offset)
    return (r, g, b)


def _is_white(rgb: tuple[int, int, int], tolerance: int = 30) -> bool:
    return all(c > (255 - tolerance) for c in rgb)


def _is_orange(rgb: tuple[int, int, int]) -> bool:
    """Detect orange-ish pixels: high red, moderate green, low blue."""
    r, g, b = rgb
    return r > 180 and g > 80 and g < 200 and b < 120


def _is_marker_red(rgb: tuple[int, int, int]) -> bool:
    """Detect red marker pixels: high red, low green and blue."""
    r, g, b = rgb
    return r > 160 and g < 80 and b < 80


class TestMakeGraphBaseline(unittest.TestCase):
    """Existing behavior is not broken by the new parameters."""

    def test_no_right_data_produces_same_size(self):
        """Passing no right_data should produce the same surface dimensions."""
        data = [[10, 20, 30, 40, 50]]
        s1 = make_graph_imagesurface(data, width=320, height=200)
        s2 = make_graph_imagesurface(data, width=320, height=200,
                                     right_data=None, markers=None)
        self.assertEqual(s1.get_width(), s2.get_width())
        self.assertEqual(s1.get_height(), s2.get_height())

    def test_basic_graph_still_works(self):
        """A plain graph with no right axis data renders without error."""
        data = [[5, 10, 15, 20, 25, 30]]
        surface = make_graph_imagesurface(data, labels=["Test"],
                                          width=200, height=100)
        self.assertEqual(surface.get_width(), 200)
        self.assertEqual(surface.get_height(), 100)


class TestDualAxis(unittest.TestCase):
    """Right Y axis rendering."""

    def test_right_data_reserves_right_margin(self):
        """With right_data, left-axis data lines should not extend as far
        right as they do without a right axis."""
        n = 20
        data = [[50] * n]
        # render without right axis — find rightmost red pixel (left data)
        s1 = make_graph_imagesurface(data, width=320, height=200, min_y_scale=100)
        rightmost_no_axis = 0
        mid_y = 100
        for x in range(319, 0, -1):
            rgb = _pixel_at(s1, x, mid_y)
            if not _is_white(rgb) and rgb != (0, 0, 0):
                rightmost_no_axis = x
                break
        # render with right axis
        right_data = [[1.0] * n]
        s2 = make_graph_imagesurface(
            data, width=320, height=200, min_y_scale=100,
            right_data=right_data, right_labels=["R"],
            right_y_range=(0.9, 1.1),
        )
        rightmost_with_axis = 0
        for x in range(319, 0, -1):
            rgb = _pixel_at(s2, x, mid_y)
            if not _is_white(rgb) and rgb != (0, 0, 0):
                rightmost_with_axis = x
                break
        # right axis labels take space, so left data should stop earlier
        self.assertGreater(rightmost_no_axis, rightmost_with_axis,
                           "Right axis margin should shrink the graph area")

    def test_right_data_renders_colored_line(self):
        """Right-axis data should draw in the right_colours color (orange)."""
        # create a graph where left data is 0 (sits at bottom) and right data
        # is at the center of the range — the orange line should appear in the
        # middle vertical region
        n = 40
        data = [[0] * n]
        right_data = [[1.0] * n]  # center of (0.9, 1.1)
        surface = make_graph_imagesurface(
            data, width=320, height=200, min_y_scale=10,
            right_data=right_data, right_labels=["Tempo"],
            right_y_range=(0.9, 1.1),
            right_colours=((0.9, 0.55, 0.0),),
        )
        # scan the middle row for orange pixels
        mid_y = 200 // 2
        found_orange = False
        for x in range(40, 280):
            rgb = _pixel_at(surface, x, mid_y)
            if _is_orange(rgb):
                found_orange = True
                break
        self.assertTrue(found_orange, "Expected orange pixels from right-axis data in the middle of the graph")


class TestStepRendering(unittest.TestCase):
    """Step-function rendering for right-axis data."""

    def test_step_function_has_horizontal_segments(self):
        """A step function that changes value should produce horizontal
        segments — adjacent pixels at the same Y when the value hasn't
        changed yet."""
        n = 100
        # first half at 1.0, second half at 1.05 — there should be a clear
        # horizontal run at each level
        right_data = [[1.0] * 50 + [1.05] * 50]
        data = [[0] * n]
        surface = make_graph_imagesurface(
            data, width=400, height=200, min_y_scale=10,
            right_data=right_data, right_labels=["Tempo"],
            right_y_range=(0.9, 1.1),
            right_colours=((0.9, 0.55, 0.0),),
            right_steps=True,
        )
        # find a horizontal run of orange pixels (at least 10 consecutive)
        max_run = 0
        for y in range(20, 180):
            run = 0
            for x in range(40, 360):
                rgb = _pixel_at(surface, x, y)
                if _is_orange(rgb):
                    run += 1
                    max_run = max(max_run, run)
                else:
                    run = 0
        self.assertGreater(max_run, 10,
                           "Expected a horizontal run of orange pixels from step rendering")


class TestEventMarkers(unittest.TestCase):
    """Underrun/overrun triangle markers."""

    def test_down_marker_draws_red_near_bottom(self):
        """A 'down' marker should draw red pixels near the bottom of the graph."""
        n = 40
        data = [[0] * n]
        markers = [(20, "down", 1)]
        surface = make_graph_imagesurface(
            data, width=320, height=200, min_y_scale=10,
            markers=markers,
        )
        # scan the bottom 20% for red pixels
        found_red = False
        for y in range(160, 195):
            for x in range(40, 280):
                rgb = _pixel_at(surface, x, y)
                if _is_marker_red(rgb):
                    found_red = True
                    break
            if found_red:
                break
        self.assertTrue(found_red, "Expected red marker pixels near the bottom of the graph")

    def test_up_marker_draws_red_near_top(self):
        """An 'up' marker should draw red pixels near the top of the graph."""
        n = 40
        data = [[0] * n]
        markers = [(20, "up", 1)]
        surface = make_graph_imagesurface(
            data, width=320, height=200, min_y_scale=10,
            markers=markers,
        )
        # scan the top 20% for red pixels
        found_red = False
        for y in range(15, 50):
            for x in range(40, 280):
                rgb = _pixel_at(surface, x, y)
                if _is_marker_red(rgb):
                    found_red = True
                    break
            if found_red:
                break
        self.assertTrue(found_red, "Expected red marker pixels near the top of the graph")

    def test_markers_without_right_data(self):
        """Markers should work even when no right_data is present."""
        n = 40
        # use blue data (colour index 1) to avoid red confusion with markers
        data = [[0] * n]
        markers = [(10, "down", 1), (30, "up", 2)]
        surface = make_graph_imagesurface(
            data, width=320, height=200, min_y_scale=10,
            colours=((0, 0, 0.8),),
            markers=markers,
        )
        found_red = False
        for y in range(15, 195):
            for x in range(40, 280):
                rgb = _pixel_at(surface, x, y)
                if _is_marker_red(rgb):
                    found_red = True
                    break
            if found_red:
                break
        self.assertTrue(found_red, "Expected red marker pixels when markers used without right_data")


class TestRightAxisLabels(unittest.TestCase):
    """Right Y axis label drawing."""

    def test_right_axis_has_nonwhite_pixels_on_right_edge(self):
        """With right_data, the rightmost margin should have non-white pixels
        from axis labels and tick marks."""
        n = 40
        data = [[50] * n]
        right_data = [[1.0] * n]
        surface = make_graph_imagesurface(
            data, width=320, height=200, min_y_scale=100,
            right_data=right_data, right_labels=["Tempo"],
            right_y_range=(0.9, 1.1),
        )
        # check the rightmost 30 pixels for any non-white
        found_nonwhite = False
        for y in range(30, 170):
            for x in range(290, 318):
                rgb = _pixel_at(surface, x, y)
                if not _is_white(rgb):
                    found_nonwhite = True
                    break
            if found_nonwhite:
                break
        self.assertTrue(found_nonwhite,
                        "Expected non-white pixels on right edge from right-axis labels")


if __name__ == "__main__":
    unittest.main()
