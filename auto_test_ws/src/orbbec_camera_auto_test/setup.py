from pathlib import Path

from setuptools import find_packages, setup


package_name = "orbbec_camera_auto_test"


def profile_data_files():
    entries = []
    for profile_path in sorted(Path("profiles").glob("**/*.yaml")):
        destination = Path(f"share/{package_name}") / profile_path.parent
        entries.append((str(destination), [str(profile_path)]))
    return entries


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
    ] + profile_data_files(),
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="slz",
    maintainer_email="slz@example.com",
    description="Automated functional and performance tests for Orbbec ROS2 camera launch files.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "run_functional_test = orbbec_camera_auto_test.runners.functional:main",
            "run_performance_test = orbbec_camera_auto_test.runners.performance:main",
            "run_restart_test = orbbec_camera_auto_test.runners.restart:main",
        ],
    },
)
