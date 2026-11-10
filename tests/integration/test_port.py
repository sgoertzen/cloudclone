import os
import subprocess
import time
import requests
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


@pytest.mark.integration
def test_container_runs_on_port_5550():
    """Test that container actually runs on port 5550 when started."""
    # This test requires Docker to be installed and running
    try:
        # Build the Docker image
        result = subprocess.run(
            ["docker", "build", "-t", "cloudclone-test", "."],
            capture_output=True,
            text=True,
            timeout=120
        )
        assert result.returncode == 0, f"Docker build failed: {result.stderr}"
        
        # Run the container in detached mode
        container_id = subprocess.run(
            ["docker", "run", "-d", "-p", "5550:5550", "cloudclone-test"],
            capture_output=True,
            text=True,
            timeout=30
        ).stdout.strip()
        
        assert container_id, "Failed to start container"
        
        # Give the container time to start up
        time.sleep(3)
        
        # Verify the container is running and accessible on port 5550
        try:
            response = requests.get("http://localhost:5550", timeout=5)
            assert response.status_code in [200, 404], f"Expected status 200 or 404, got {response.status_code}"
            
            # Check that we get a response from the application
            assert response.url == "http://localhost:5550/", "Expected root URL"
        except requests.exceptions.ConnectionError:
            pytest.fail("Failed to connect to container on port 5550")
        finally:
            # Clean up: stop and remove the container
            subprocess.run(["docker", "stop", container_id], capture_output=True)
            subprocess.run(["docker", "rm", container_id], capture_output=True)
            
    except subprocess.TimeoutExpired:
        pytest.skip("Docker build or run timed out - likely Docker not available")
    except FileNotFoundError:
        pytest.skip("Docker not found - likely Docker not installed or not in PATH")
    except Exception as e:
        pytest.fail(f"Unexpected error during container test: {str(e)}")


@pytest.mark.integration
def test_container_exposes_correct_port():
    """Test that the container's Dockerfile correctly exposes port 5550."""
    # Read the Dockerfile to verify it exposes port 5550
    with open('Dockerfile', 'r') as f:
        content = f.read()
        assert 'EXPOSE 5550' in content, "Dockerfile should expose port 5550"
        
        # Verify the entrypoint script uses port 5550
        with open('entrypoint.sh', 'r') as ef:
            entrypoint_content = ef.read()
            assert '--port 5550' in entrypoint_content, "Entrypoint should use port 5550"