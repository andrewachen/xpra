#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2026 Netflix, Inc.
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

# ABOUTME: Tests for add_build_info.py's parse_describe — verifies that the
# ABOUTME: 'git describe' revision parser handles tag names containing hyphens.

import os
import unittest
import importlib.util


def _load_add_build_info():
    # add_build_info.py lives in fs/bin/, outside the importable package tree
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    path = os.path.join(root, "fs", "bin", "add_build_info.py")
    spec = importlib.util.spec_from_file_location("add_build_info", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestParseDescribe(unittest.TestCase):
    """parse_describe must split the count and commit off the right-hand side,
    so that tag names containing hyphens are handled correctly."""

    def setUp(self):
        self.parse_describe = _load_add_build_info().parse_describe

    def test_plain_tag(self):
        # upstream-style dotted tag, no internal hyphens
        self.assertEqual(self.parse_describe("v4.0.6-58-g6e6614571"), ("58", "g6e6614571"))

    def test_hyphenated_tag(self):
        # checkpoint tags on the fork contain hyphens; the count/commit are on the right
        self.assertEqual(self.parse_describe("v6.5-achen-substream-guard-0-ga45131099c"),
                         ("0", "ga45131099c"))

    def test_bare_commit_no_tags(self):
        # 'git describe --always' with no reachable tag yields a bare abbreviated SHA
        self.assertEqual(self.parse_describe("a45131099c"), ("0", "a45131099c"))

    def test_unparseable(self):
        self.assertIsNone(self.parse_describe("v6.5-achen"))


if __name__ == "__main__":
    unittest.main()
