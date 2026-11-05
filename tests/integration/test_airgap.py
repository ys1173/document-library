"""Criterion 6. The test container is on an internal Docker network, so this checks the network really is closed."""

from __future__ import annotations

import os
import socket

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("DOCLIB_TEST_AIRGAP"), reason="needs the internal network")]


@pytest.mark.parametrize("host", ["1.1.1.1", "8.8.8.8", "huggingface.co", "downloads.rclone.org"])
def test_no_route_to_the_internet(host):
    with pytest.raises(OSError):
        socket.create_connection((host, 443), timeout=3).close()
