
from setuptools import Extension, find_packages, setup
from Cython.Build import cythonize

extensions = [
    Extension(
        name="kimi.extractor.youtube_core",
        sources=["kimi/extractor/youtube_core.pyx"],
    )
]

setup(
    name="kimi-dlp",
    version="0.1.0",
    packages=find_packages(),
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": 3,
            "boundscheck": False,
            "wraparound": False,
            "initializedcheck": False,
            "cdivision": True,
        },
    ),
    zip_safe=False,
)
