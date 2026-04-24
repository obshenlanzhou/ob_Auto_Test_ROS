from glob import glob

from setuptools import find_packages, setup


package_name = "orbbec_camera_auto_test"


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (
            f"share/{package_name}/profiles/functional",
            glob("profiles/functional/*.yaml"),
        ),
        (
            f"share/{package_name}/profiles/performance",
            glob("profiles/performance/*.yaml"),
        ),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="slz",
    maintainer_email="slz@example.com",
    description="Automated functional and performance tests for Orbbec ROS2 camera launch files.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "run_functional_test = orbbec_camera_auto_test.functional_runner:main",
            "run_performance_test = orbbec_camera_auto_test.performance_runner:main",
        ],
    },
)
