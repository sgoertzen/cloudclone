#!/usr/bin/env python3
"""
Test runner script for CloudClone project.
This script provides convenient ways to run unit and integration tests.
"""

import subprocess
import sys

def run_tests(test_type):
    """Run tests of the specified type (unit or integration)."""
    try:
        if test_type == "unit":
            result = subprocess.run([
                sys.executable, "-m", "pytest", 
                "tests/unit", 
                "-v"
            ], check=True)
        elif test_type == "integration":
            result = subprocess.run([
                sys.executable, "-m", "pytest", 
                "tests/integration", 
                "-v"
            ], check=True)
        elif test_type == "all":
            result = subprocess.run([
                sys.executable, "-m", "pytest", 
                "tests/unit", 
                "tests/integration",
                "-v"
            ], check=True)
        else:
            print(f"Unknown test type: {test_type}")
            return False
            
        return True
    except subprocess.CalledProcessError as e:
        print(f"Tests failed with exit code {e.returncode}")
        return False

def main():
    """Main function to handle command line arguments."""
    if len(sys.argv) < 2:
        print("Usage: python run_tests.py [unit|integration|all]")
        print("Examples:")
        print("  python run_tests.py unit        # Run only unit tests")
        print("  python run_tests.py integration # Run only integration tests")
        print("  python run_tests.py all         # Run all tests")
        return
        
    test_type = sys.argv[1].lower()
    success = run_tests(test_type)
    
    if success:
        print(f"All {test_type} tests passed!")
    else:
        print(f"Some {test_type} tests failed!")
        sys.exit(1)

if __name__ == "__main__":
    main()