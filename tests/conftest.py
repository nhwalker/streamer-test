"""Shared unit-test setup: make service/ modules importable by bare name.

Every module under test lives in service/ (which has no package
__init__); putting it on sys.path once here replaces a copy of this
boilerplate at the top of every test file.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'service'))
