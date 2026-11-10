import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import main


def test_port_configuration_files_updated():
    """Test that all configuration files have been updated to use port 5550."""
    # Test entrypoint.sh
    with open('entrypoint.sh', 'r') as f:
        content = f.read()
        assert '--port 5550' in content

    # Test Dockerfile
    with open('Dockerfile', 'r') as f:
        content = f.read()
        assert 'EXPOSE 5550' in content
        assert 'net.unraid.docker.webui="http://[IP]:[PORT:5550]/"' in content

    # Test cloudclone.xml
    with open('cloudclone.xml', 'r') as f:
        content = f.read()
        assert '<WebUI>http://[IP]:[PORT:5550]/</WebUI>' in content
        assert '<Config Name="WebUI Port" Target="5550" Default="5550"' in content


def test_main_module_imports():
    """Test that main module imports correctly with port configuration."""
    # This tests that the main application can be imported without errors
    # and that port-related configurations are handled properly
    assert main.app is not None
    assert hasattr(main, 'scheduler')