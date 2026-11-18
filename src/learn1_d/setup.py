import os
from glob import glob

from setuptools import find_packages, setup

package_name = "learn1_d"

setup(
    name=package_name,
    version="0.0.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages",
         ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
        (os.path.join("share", package_name, "launch"),
         glob(os.path.join("launch", "*.launch.py"))),
        # 导览路线与文案；--config 指向的其它 json 由用户自己保管。
        (os.path.join("share", package_name, "tour"), glob("learn1_d/tour.json")),
    ],
    package_data={package_name: ["*.json"]},
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="nvidia",
    maintainer_email="nvidia@todo.todo",
    description="ROS 2 学习包：底盘运动、地图点位/轨道导航与 TTS 播报测试",
    license="TODO: License declaration",
    extras_require={
        "test": [
            "pytest",
        ],
    },
    entry_points={
        "console_scripts": [
            "speak_test = learn1_d.speak_test:main",
            "map_nav_test = learn1_d.map_nav_test:main",
        ],
    },
)
