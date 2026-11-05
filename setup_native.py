"""Build the optional accelerator: python setup_native.py build_ext --inplace."""
from setuptools import setup
from pybind11.setup_helpers import Pybind11Extension, build_ext

setup(
    name="turkish-news-native",
    version="0.1.0",
    ext_modules=[Pybind11Extension("_news_native", ["native/text.cpp"], cxx_std=11)],
    cmdclass={"build_ext": build_ext},
)
